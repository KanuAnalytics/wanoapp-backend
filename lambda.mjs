import { spawn } from "node:child_process";
import { createWriteStream } from "node:fs";
import { open, readFile, stat } from "node:fs/promises";
import { extname } from "node:path";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";

const FFMPEG_PATH = "/opt/bin/ffmpeg";
const FFPROBE_PATH = "/opt/bin/ffprobe";
const TUS_CHUNK_SIZE = 50 * 1024 * 1024; // 50MB
const HLS_DOWNLOAD_CONCURRENCY = 8;
const BACKEND_API_URL = process.env.BACKEND_API_URL;

const TARGET_WIDTH = 1080;
const TARGET_HEIGHT = 1920;
const TARGET_FPS = 30;
const TARGET_SAMPLE_RATE = 44100;

async function downloadFromUrl(url, destPath) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to download ${url}: ${res.status}`);
  await pipeline(Readable.fromWeb(res.body), createWriteStream(destPath));
}

async function fetchText(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch ${url}: ${res.status}`);
  return res.text();
}

async function fetchBuffer(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch ${url}: ${res.status}`);
  return Buffer.from(await res.arrayBuffer());
}

// Picks the highest-resolution video track and the default audio track from an HLS master manifest.
function parseMasterManifest(text, masterUrl) {
  const lines = text.split("\n").map((l) => l.trim());

  let best = null;
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].startsWith("#EXT-X-STREAM-INF")) continue;
    const res = lines[i].match(/RESOLUTION=(\d+)x(\d+)/);
    const pixels = res ? Number(res[1]) * Number(res[2]) : 0;
    const uri = lines.slice(i + 1).find((l) => l && !l.startsWith("#"));
    if (uri && (!best || pixels > best.pixels)) best = { pixels, url: new URL(uri, masterUrl).href };
  }
  if (!best) throw new Error(`No video tracks found in manifest ${masterUrl}`);

  const audioLines = lines.filter((l) => l.startsWith("#EXT-X-MEDIA") && l.includes("TYPE=AUDIO") && l.includes('URI="'));
  const audioLine = audioLines.find((l) => l.includes("DEFAULT=YES")) ?? audioLines[0];
  const audioUrl = audioLine ? new URL(audioLine.match(/URI="([^"]+)"/)[1], masterUrl).href : null;

  return { videoUrl: best.url, audioUrl };
}

// Saves one HLS track as a single local MP4 (init section followed by every segment, in order).
async function downloadHlsTrack(playlistUrl, destPath) {
  const lines = (await fetchText(playlistUrl)).split("\n").map((l) => l.trim());

  const mapLine = lines.find((l) => l.startsWith("#EXT-X-MAP"));
  if (!mapLine) throw new Error(`Unsupported HLS track (no MP4 init section): ${playlistUrl}`);
  const initUrl = new URL(mapLine.match(/URI="([^"]+)"/)[1], playlistUrl).href;

  const segmentUrls = lines.filter((l) => l && !l.startsWith("#")).map((l) => new URL(l, playlistUrl).href);
  if (segmentUrls.length === 0) throw new Error(`HLS track has no segments: ${playlistUrl}`);

  const duration = lines
    .filter((l) => l.startsWith("#EXTINF:"))
    .reduce((sum, l) => sum + parseFloat(l.slice("#EXTINF:".length)), 0);

  const file = await open(destPath, "w");
  try {
    await file.write(await fetchBuffer(initUrl));
    for (let i = 0; i < segmentUrls.length; i += HLS_DOWNLOAD_CONCURRENCY) {
      const batch = await Promise.all(segmentUrls.slice(i, i + HLS_DOWNLOAD_CONCURRENCY).map(fetchBuffer));
      for (const buf of batch) await file.write(buf);
    }
  } finally {
    await file.close();
  }

  return { duration, segments: segmentUrls.length };
}

function runFfmpeg(args) {
  return new Promise((resolve, reject) => {
    const proc = spawn(FFMPEG_PATH, args);
    let stderr = "";
    proc.stderr.on("data", (d) => (stderr += d.toString()));
    proc.on("error", reject);
    proc.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg exited ${code}: ${stderr}`))));
  });
}

function runFfprobe(args) {
  return new Promise((resolve, reject) => {
    const proc = spawn(FFPROBE_PATH, args);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d.toString()));
    proc.stderr.on("data", (d) => (stderr += d.toString()));
    proc.on("error", reject);
    proc.on("close", (code) => (code === 0 ? resolve(stdout.trim()) : reject(new Error(`ffprobe exited ${code}: ${stderr}`))));
  });
}

async function getDuration(filePath) {
  const out = await runFfprobe([
    "-v", "error",
    "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1",
    filePath,
  ]);
  return parseFloat(out);
}

async function hasAudioStream(filePath) {
  const out = await runFfprobe([
    "-v", "error",
    "-select_streams", "a",
    "-show_entries", "stream=index",
    "-of", "csv=p=0",
    filePath,
  ]);
  return out.trim().length > 0;
}

// music: { path, keepOriginalSound } or null. The song loops to cover the whole stitched video.
function buildConcatFilterArgs(clips, outputPath, music = null) {
  const useClipSound = !music || music.keepOriginalSound;
  const totalDuration = clips.reduce((sum, clip) => sum + clip.segmentDuration, 0);
  const inputArgs = [];
  const filters = [];
  let inputIndex = 0;

  clips.forEach((clip, i) => {
    let videoRef;
    let audioRef = null;
    let videoTrim = "";
    let audioTrim = "";

    if (clip.kind === "hls") {
      // Trimming happens inside the filter graph so picture and sound are cut on the same clock.
      inputArgs.push("-i", clip.videoPath);
      videoRef = `[${inputIndex++}:v:0]`;
      if (useClipSound && clip.audioPath) {
        inputArgs.push("-i", clip.audioPath);
        audioRef = `[${inputIndex++}:a:0]`;
      }
      videoTrim = `trim=start=${clip.start}:duration=${clip.segmentDuration},setpts=PTS-STARTPTS,`;
      audioTrim = `atrim=start=${clip.start}:duration=${clip.segmentDuration},asetpts=PTS-STARTPTS,`;
    } else {
      inputArgs.push("-ss", String(clip.start), "-t", String(clip.segmentDuration), "-i", clip.path);
      videoRef = `[${inputIndex}:v:0]`;
      if (clip.hasAudio) audioRef = `[${inputIndex}:a:0]`;
      inputIndex++;
    }

    filters.push(
      `${videoRef}${videoTrim}scale=w=${TARGET_WIDTH}:h=${TARGET_HEIGHT}:force_original_aspect_ratio=increase,crop=${TARGET_WIDTH}:${TARGET_HEIGHT},setsar=1,fps=${TARGET_FPS}[v${i}]`,
    );
    if (!useClipSound) return;
    filters.push(
      audioRef
        ? `${audioRef}${audioTrim}aformat=sample_rates=${TARGET_SAMPLE_RATE}:channel_layouts=stereo[a${i}]`
        : `anullsrc=r=${TARGET_SAMPLE_RATE}:cl=stereo,atrim=duration=${clip.segmentDuration}[a${i}]`,
    );
  });

  const clipSoundLabel = music ? "[clipsound]" : "[outa]";
  if (useClipSound) {
    const streamRefs = clips.map((_, i) => `[v${i}][a${i}]`).join("");
    filters.push(`${streamRefs}concat=n=${clips.length}:v=1:a=1[outv]${clipSoundLabel}`);
  } else {
    const streamRefs = clips.map((_, i) => `[v${i}]`).join("");
    filters.push(`${streamRefs}concat=n=${clips.length}:v=1:a=0[outv]`);
  }

  if (music) {
    inputArgs.push("-stream_loop", "-1", "-i", music.path);
    const musicLabel = music.keepOriginalSound ? "[music]" : "[outa]";
    filters.push(
      `[${inputIndex++}:a:0]aformat=sample_rates=${TARGET_SAMPLE_RATE}:channel_layouts=stereo,atrim=duration=${totalDuration},asetpts=PTS-STARTPTS${musicLabel}`,
    );
    if (music.keepOriginalSound) filters.push("[clipsound][music]amix=inputs=2:duration=first[outa]");
  }

  return [
    ...inputArgs,
    "-filter_complex", filters.join(";"),
    "-map", "[outv]",
    "-map", "[outa]",
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-c:a", "aac",
    "-movflags", "+faststart",
    ...(music ? ["-t", String(totalDuration)] : []),
    outputPath,
    "-y",
  ];
}

async function getPresignedUploadUrl(filename, fileSize, folder = "videos") {
  const res = await fetch(`${BACKEND_API_URL}/video/v2/presigned-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, fileSize, folder }),
  });
  if (!res.ok) throw new Error(`Presign request failed: ${res.status} ${await res.text()}`);
  const json = await res.json();
  return json.data;
}

async function uploadViaTus(filePath, uploadUrl) {
  const buffer = await readFile(filePath);
  let offset = 0;

  while (offset < buffer.length) {
    const chunk = buffer.subarray(offset, offset + TUS_CHUNK_SIZE);
    const res = await fetch(uploadUrl, {
      method: "PATCH",
      headers: {
        "Tus-Resumable": "1.0.0",
        "Upload-Offset": String(offset),
        "Content-Type": "application/offset+octet-stream",
      },
      body: chunk,
    });
    if (!res.ok) throw new Error(`TUS PATCH failed at offset ${offset}: ${res.status} ${await res.text()}`);
    offset = Number(res.headers.get("Upload-Offset"));
  }

  return uploadUrl;
}

function parseEventBody(event) {
  if (typeof event.body !== "string") return event;
  const raw = event.isBase64Encoded ? Buffer.from(event.body, "base64").toString("utf8") : event.body;
  return JSON.parse(raw);
}

// Accepts either "https://..." or { url, start?, end? } (seconds).
function normalizeClip(item) {
  if (typeof item === "string") return { url: item, start: 0, end: null };
  return { url: item.url, start: Number(item.start ?? 0), end: item.end == null ? null : Number(item.end) };
}

async function prepareClip(clip, i) {
  if (clip.url.includes(".m3u8")) {
    const { videoUrl, audioUrl } = parseMasterManifest(await fetchText(clip.url), clip.url);
    const videoPath = `/tmp/input_${i}_video.mp4`;
    const audioPath = audioUrl ? `/tmp/input_${i}_audio.mp4` : null;
    const [video] = await Promise.all([
      downloadHlsTrack(videoUrl, videoPath),
      audioUrl ? downloadHlsTrack(audioUrl, audioPath) : null,
    ]);
    return { kind: "hls", videoPath, audioPath, fullDuration: video.duration, hasAudio: Boolean(audioPath) };
  }

  const path = `/tmp/input_${i}.mp4`;
  await downloadFromUrl(clip.url, path);
  return { kind: "mp4", path, fullDuration: await getDuration(path), hasAudio: await hasAudioStream(path) };
}

export const handler = async (event) => {
  const { videoUrls, audio, keepOriginalSound = false, filename = "stitched.mp4", folder = "videos" } = parseEventBody(event);

  if (!BACKEND_API_URL) throw new Error("BACKEND_API_URL env var is not set");

  const clips = [];
  for (let i = 0; i < videoUrls.length; i++) {
    const requested = normalizeClip(videoUrls[i]);
    const prepared = await prepareClip(requested, i);

    const start = Math.max(0, Math.min(requested.start, prepared.fullDuration));
    const end = requested.end == null ? prepared.fullDuration : Math.min(requested.end, prepared.fullDuration);
    const segmentDuration = end - start;
    if (segmentDuration <= 0) throw new Error(`input_${i}: invalid segment ${start}s -> ${end}s (clip is ${prepared.fullDuration}s)`);

    console.log(`input_${i} (${prepared.kind}) full: ${prepared.fullDuration}s, segment: ${start}s -> ${end}s (${segmentDuration}s), hasAudio: ${prepared.hasAudio}`);
    clips.push({ ...prepared, start, segmentDuration });
  }

  let music = null;
  if (audio?.uri) {
    const path = `/tmp/music${extname(new URL(audio.uri).pathname) || ".mp3"}`;
    await downloadFromUrl(audio.uri, path);
    music = { path, keepOriginalSound: Boolean(keepOriginalSound) };
    console.log(`music: "${audio.title ?? audio.uri}", keepOriginalSound: ${music.keepOriginalSound}`);
  }

  const outputPath = "/tmp/output.mp4";
  await runFfmpeg(buildConcatFilterArgs(clips, outputPath, music));

  const outputDuration = await getDuration(outputPath);
  console.log(`output.mp4 duration: ${outputDuration}s`);

  const { size: fileSize } = await stat(outputPath);
  console.log(`output.mp4 fileSize: ${fileSize} bytes`);

  const { upload_url: uploadUrl, stream_uid, file_url } = await getPresignedUploadUrl(filename, fileSize, folder);

  await uploadViaTus(outputPath, uploadUrl);

  return {
    statusCode: 200,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stream_uid, file_url }),
  };
};
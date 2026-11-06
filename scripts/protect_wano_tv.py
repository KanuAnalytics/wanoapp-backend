"""
Makes Wano TV film files private on Spaces (trailers and artwork stay public).
Run only after the signed-playback backend is deployed and a film plays on a phone.

    python -m scripts.protect_wano_tv            # dry run: lists what would change
    python -m scripts.protect_wano_tv --apply

Undo (if ever needed): set the same keys back to public-read.
"""

import sys

from app.services.tv_stream import SpacesAdapter, protect_films


def main():
    apply = "--apply" in sys.argv
    result = protect_films(SpacesAdapter(), apply=apply)
    for k in result["private"]:
        print(("made private: " if apply else "would make private: ") + k)
    print(f"{len(result['private'])} files {'made private' if apply else 'to make private (dry run)'}")


if __name__ == "__main__":
    main()

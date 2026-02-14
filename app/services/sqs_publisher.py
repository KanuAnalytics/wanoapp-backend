import json
from app.core.config import settings
import boto3


def push_video_processing_job(
    videoId: str,
    uId: str,
):
    """
    Push a video processing job to SQS
    """
    
    sqs_client = boto3.client(
        "sqs",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )

    message_body = {
        "videoId": videoId,
        "uId": uId,
    }

    response = sqs_client.send_message(
        QueueUrl=settings.SQS_VIDEO_QUEUE_URL,
        MessageBody=json.dumps(message_body),
    )

    return response
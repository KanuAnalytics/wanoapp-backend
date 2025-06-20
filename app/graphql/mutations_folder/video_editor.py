
"""
app/graphql/mutations_folder/video_editor.py
"""

import strawberry
import httpx
import asyncio
from typing import Dict, Any
from app.graphql.types import ResponseType
from app.graphql.inputs.video_editor import CompileVideoInput
from fastapi import HTTPException, BackgroundTasks
from app.core.config import settings

# this is for updating the Deployment
async def call_video_service_api(input: CompileVideoInput, user_id: str) -> Dict[str, Any]:
        """
        Call the second backend's GraphQL API
        """
        # GraphQL endpoint URL of your second backend
        SERVICE_API_URL = settings.VIDEO_SERVICE_URL  # Update this URL
        
        # Prepare the GraphQL mutation query
        mutation = """
        mutation CompileVideo($input: CompileVideoInput!) {
            compileVideo(input: $input) {
                remoteUrl
                duration
                start
                end
                type
            }
        }
        """
        
        # Convert the input to include user_id
        variables = {
            "input": {
                "video": [
                    {
                        "FEid": video.FEid,
                        "duration": video.duration,
                        "start": video.start,
                        "end": video.end,
                        "remoteUrl": video.remoteUrl,
                        "type": video.type,
                        "index": video.index,
                        "isTrimmed": video.isTrimmed
                    }
                    for video in input.video
                ],
                "audioUrl": input.audio_url,
                "ratio": input.ratio,
                "videoType": input.videoType,
                "description": input.description,
                "userId": user_id  # Add the user_id field
            }
        }
        
        # Prepare the request payload
        payload = {
            "query": mutation,
            "variables": variables
        }
        
        # Make the HTTP request to the second backend with longer timeout
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    SERVICE_API_URL,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        # Add any authentication headers if needed
                        # "Authorization": f"Bearer {token}",
                    },
                    timeout=600.0  # 10 minutes timeout for long-running operations
                )
                
                # Check if the request was successful
                response.raise_for_status()
                
                # Parse the response
                response_data = response.json()
                
                # Check for GraphQL errors
                if "errors" in response_data:
                    error_messages = [error.get("message", "Unknown error") for error in response_data["errors"]]
                    raise Exception(f"GraphQL errors: {', '.join(error_messages)}")
                
                # Extract the data from the response
                if "data" in response_data and "compileVideo" in response_data["data"]:
                    return response_data["data"]["compileVideo"]
                else:
                    raise Exception("Invalid response format from service API")
                    
            except httpx.TimeoutException:
                raise Exception("Request to service API timed out")
            except httpx.HTTPStatusError as e:
                raise Exception(f"HTTP error from service API: {e.response.status_code}")
            except Exception as e:
                raise Exception(f"Error calling service API: {str(e)}")

async def call_video_service_api_background(input: CompileVideoInput, user_id: str):
        """
        Background task to call the second backend's GraphQL API
        This runs independently and doesn't block the main response
        """
        try:
            await call_video_service_api(input, user_id)
            # Optionally: Store success status in database, send notification, etc.
            print(f"Video compilation completed successfully for user {user_id}")
            
        except Exception as e:
            # Handle errors in background task
            # Optionally: Store error status in database, send error notification, etc.
            print(f"Video compilation failed for user {user_id}: {str(e)}")
            # You might want to log this properly or store in database

@strawberry.type
class VideoEditorMutation:
    @strawberry.mutation
    async def compile_video(self, info, input: CompileVideoInput) -> ResponseType:
        try:
            # Check authentication
            user_id = info.context.get("user_id")
            if not user_id:
                raise Exception("Authentication required")
            
            print(user_id)
            
            # Fire and forget - start the background task without waiting
            asyncio.create_task(call_video_service_api_background(input, user_id))
            
            # Return immediately with a success response
            return ResponseType(
                message="Video compilation request submitted successfully. Processing in background.",
                status="accepted"
            )
            
        except HTTPException:
            # Re-raise HTTP exceptions
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")
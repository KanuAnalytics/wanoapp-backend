# Create a test script: test_connection.py

import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

async def test_connection():
    # Print raw environment variables
    print("=== Environment Variables ===")
    print(f"MONGODB_URL: '{os.getenv('MONGODB_URL')}'")
    print(f"DATABASE_NAME: '{os.getenv('DATABASE_NAME')}'")
    print(f"DATABASE_NAME repr: {repr(os.getenv('DATABASE_NAME'))}")
    print(f"DATABASE_NAME length: {len(os.getenv('DATABASE_NAME'))}")
    print("=" * 30)
    
    # Try direct connection
    try:
        mongodb_url = os.getenv('MONGODB_URL', 'mongodb://localhost:27017')
        database_name = os.getenv('DATABASE_NAME', 'wanoapp')
        
        print(f"\nConnecting to: {mongodb_url}")
        print(f"Database: '{database_name}'")
        
        client = AsyncIOMotorClient(mongodb_url)
        db = client[database_name]
        
        # Test connection
        await client.server_info()
        print("\n✅ Connection successful!")
        
        # List databases
        db_list = await client.list_database_names()
        print(f"\nAvailable databases: {db_list}")
        
        client.close()
        
    except Exception as e:
        print(f"\n❌ Connection failed: {e}")
        print(f"Error type: {type(e)}")

if __name__ == "__main__":
    asyncio.run(test_connection())
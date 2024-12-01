import dramatiq
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from dramatiq import Middleware
from ..models import Broadcast  # Adjust this import as needed based on your project structure
import requests
import json
from dramatiq.middleware import Middleware,SkipMessage
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from ..database import database
import httpx
from ..models.ChatBox import Conversation
from datetime import datetime
from sqlalchemy.future import select
from dramatiq.middleware import AgeLimit, TimeLimit, Retries
from dramatiq.middleware import AsyncIO
import asyncio
# SQLAlchemy Database Configuration
SQLALCHEMY_DATABASE_URL = 'postgresql+asyncpg://postgres:Denmarks123$@localhost/wati_clone'

engine = create_async_engine(SQLALCHEMY_DATABASE_URL, echo=True)





AsyncSessionLocal = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# Base class for declarative models
Base = declarative_base()


# Function to get task status
async def get_task_status(task_id: int, db: AsyncSession):
    """
    Fetches the status of a task based on the task_id from the database.
    """
    result = await db.execute(select(Broadcast.BroadcastList).filter(Broadcast.BroadcastList.task_id == task_id))
    broadcast=result.scalars().first()

    if broadcast:
        return broadcast.status
    
    return "unknown"
from dramatiq.middleware import Middleware
from dramatiq.middleware import SkipMessage
import asyncio

class CancelationMiddleware(Middleware):
    def __init__(self, db_session_factory):
        """
        Initialize the middleware with a database session factory.
        
        Args:
            db_session_factory: A callable that provides a database session (e.g., get_db).
        """
        self.db_session_factory = db_session_factory

    def before_process_message(self, broker, message):
        """
        Middleware hook to run before processing a message.

        Args:
            broker: The broker instance.
            message: The message being processed.
        """
        loop = self._get_or_create_event_loop()
        task_status = loop.run_until_complete(self._check_task_status(message.message_id))
        
        if task_status == "Cancelled":
            raise SkipMessage("Task has been cancelled.")

    async def _check_task_status(self, task_id):
        """
        Check the status of a task from the database asynchronously.

        Args:
            task_id: The ID of the task to check.

        Returns:
            str: The status of the task.
        """
        async for db in self.db_session_factory():
            return await get_task_status(task_id, db)

    @staticmethod
    def _get_or_create_event_loop():
        """
        Get the current event loop, or create a new one if none exists in the current thread.
        
        Returns:
            asyncio.AbstractEventLoop: The event loop for the current thread.
        """
        try:
            return asyncio.get_event_loop()
        except RuntimeError:  # No event loop in this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop


cancelation_middleware = CancelationMiddleware(get_db)

# Add the middleware to your Dramatiq broker
from dramatiq.brokers.redis import RedisBroker

redis_broker = RedisBroker(url="redis://localhost:6379")

redis_broker.add_middleware(AsyncIO()) 
redis_broker.add_middleware(cancelation_middleware)
dramatiq.set_broker(redis_broker)




@dramatiq.actor(max_retries=0)
async def send_broadcast(
    template_name,
    template_data, 
    recipients, 
    broadcastId, 
    API_url,
    headers, 
    user_id, 
    image_id, 
    body_parameters,
    Phone_id):
    """
    Dramatiq actor to send broadcast messages.
    """
    db = await anext(get_db())  # Get the db session from the async generator
    try:
        success_count = 0
        failed_count = 0
        errors = []

        async with httpx.AsyncClient() as client:
            for contact in recipients:
                recipient_name = contact["name"]
                recipient_phone = contact["phone"]

                data = {
                    "messaging_product": "whatsapp",
                    "to": recipient_phone,
                    "type": "template",
                    "template": {
                        "name": template_name,
                        "language": {"code": "en_US"},
                    }
                }

                if image_id:
                    data["template"]["components"] = [
                        {
                            "type": "header",
                            "parameters": [
                                {
                                    "type": "image",
                                    "image": {"id": image_id}
                                }
                            ]
                        }
                    ]

                if body_parameters:
                    body_params = [{"type": "text", "text": recipient_name if body_parameters == "Name" else ""}]
                    if "components" not in data["template"]:
                        data["template"]["components"] = []
                    data["template"]["components"].append({
                        "type": "body",
                        "parameters": body_params
                    })

                response = await client.post(API_url, headers=headers, data=json.dumps(data))
                response_data = response.json()

                if response.status_code == 200:
                    success_count += 1
                    wamid = response_data['messages'][0]['id']
                    phone_num = response_data['contacts'][0]["wa_id"]

                    MessageIdLog = Broadcast.BroadcastAnalysis(
                        user_id=user_id,
                        broadcast_id=broadcastId,
                        error_reason="",
                        message_id=wamid,
                        status="sent",
                        phone_no=phone_num,
                        contact_name=recipient_name
                    )
                    db.add(MessageIdLog)
                    await db.commit()
                    await db.refresh(MessageIdLog)

                    # Save the sent message data in conversations table
                    conversation = Conversation(
                        wa_id=recipient_phone,
                        message_id=wamid,
                        media_id="",
                        phone_number_id=Phone_id,
                        message_content=f"#template_message# {template_data}",
                        timestamp=datetime.utcnow(),
                        context_message_id=None,
                        message_type="text",
                        direction="sent"
                    )
                    db.add(conversation)
                    await db.commit()
                    await db.refresh(conversation)


                else:
                    failed_count += 1
                    error_detail = response_data.get("error", {}).get("message", "Unknown error")
                    error_code = response_data.get("error", {}).get("code", "N/A")
                    error_reason = f"Error Code: {error_code}, Detail: {error_detail}"
                
                    errors.append({"recipient": recipient_phone, "error": response_data})
                    
                    MessageIdLog = Broadcast.BroadcastAnalysis(
                        user_id=user_id,
                        broadcast_id=broadcastId,
                        status="failed",
                        phone_no=recipient_phone,
                        contact_name=recipient_name,
                        error_reason=error_reason
                    )
                    db.add(MessageIdLog)
                    await db.commit()
                    await db.refresh(MessageIdLog)

        broadcastLog = await db.get(Broadcast.BroadcastList, broadcastId)
        if not broadcastLog:
            raise Exception(f"Broadcast not found for ID {broadcastId}")

        broadcastLog.success = success_count
        broadcastLog.status = "Successful" if success_count > 0 else "Failed"
        broadcastLog.failed = failed_count

        db.add(broadcastLog)
        await db.commit()
        await db.refresh(broadcastLog)

        if errors:
            print(f"Failed to send some messages: {errors}")
            raise Exception(f"Failed to send broadcast: {errors}")

        print(f"Successfully sent {success_count} messages.")
        
    except Exception as e:
        await db.rollback()  # Rollback in case of an error
        print(f"Error in broadcast: {str(e)}")
        raise e
    finally:
        await db.close()  # Ensure db is closed

   




@dramatiq.actor(max_retries=0)
async def send_template_messages_task(
    broadcast_id: int,
    recipients: list,
    template: str,
    template_data:str,
    image_id: str,
    body_parameters: str,
    phone_id: str,
    access_token: str,
    user_id: int,
):
    db = await anext(get_db())
    try:
        success_count = 0
        failed_count = 0
        errors = []
        
        API_url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            for contact in recipients:
                recipient_name = contact["name"]
                recipient_phone = contact["phone"]

                data = {
                    "messaging_product": "whatsapp",
                    "to": recipient_phone,
                    "type": "template",
                    "template": {
                        "name": template,
                        "language": {"code": "en_US"},
                    }
                }

                if image_id:
                    data["template"]["components"] = [
                        {
                            "type": "header",
                            "parameters": [
                                {
                                    "type": "image",
                                    "image": {"id": image_id}
                                }
                            ]
                        }
                    ]

                if body_parameters:
                    body_params = [{"type": "text", "text": f"{recipient_name}"}] if body_parameters == "Name" else []
                    if "components" not in data["template"]:
                        data["template"]["components"] = []
                    data["template"]["components"].append({
                        "type": "body",
                        "parameters": body_params
                    })

                response = await client.post(API_url, headers=headers, json=data)
                response_data = response.json()

                if response.status_code == 200:
                    success_count += 1
                    wamid = response_data['messages'][0]['id']
                    phone_num = response_data['contacts'][0]["wa_id"]

                    message_log = Broadcast.BroadcastAnalysis(
                        user_id=user_id,
                        broadcast_id=broadcast_id,
                        message_id=wamid,
                        error_reason="",
                        status="sent",
                        phone_no=phone_num,
                        contact_name=recipient_name,
                    )
                    db.add(message_log)
                    await db.commit()
                    await db.refresh(message_log)

                    # Save the sent message data in conversations table
                    conversation = Conversation(
                        wa_id=recipient_phone,
                        message_id=wamid,
                        media_id="",
                        phone_number_id=phone_id,
                        message_content=f"#template_message# {template_data}",
                        timestamp=datetime.utcnow(),
                        context_message_id=None,
                        message_type="text",
                        direction="sent"
                    )
                    db.add(conversation)
                    await db.commit()
                    await db.refresh(conversation)

                else:
                    failed_count += 1
                    error_detail = response_data.get("error", {}).get("message", "Unknown error")
                    error_code = response_data.get("error", {}).get("code", "N/A")
                    error_reason = f"Error Code: {error_code}, Detail: {error_detail}"

                    errors.append({"recipient": recipient_phone, "error": response_data})

                    message_log = Broadcast.BroadcastAnalysis(
                        user_id=user_id,
                        broadcast_id=broadcast_id,
                        status="failed",
                        phone_no=recipient_phone,
                        contact_name=recipient_name,
                        error_reason=error_reason  # Log error details here
                    )
                    db.add(message_log)
                    await db.commit()
                    await db.refresh(message_log)


        # Update broadcast status
        broadcast = await db.get(Broadcast.BroadcastList,broadcast_id)
        if not broadcast:
            raise Exception(f"Broadcast not found for ID {broadcast_id}")
        broadcast.success = success_count
        broadcast.status = "Successful" if failed_count == 0 else "Partially Successful"
        broadcast.failed = failed_count

        db.add(broadcast)
        await db.commit()
        await db.refresh(broadcast)           

        if errors:
            print(f"Failed to send some messages: {errors}")
            raise Exception(f"Failed to send broadcast: {errors}")
        
        print(f"Successfully sent {success_count} messages.")
        
    except Exception as e:
        await db.rollback()  # Rollback in case of an error
        print(f"Error in broadcast: {str(e)}")
        raise e
    finally:
        await db.close()  # Ensure db is closed

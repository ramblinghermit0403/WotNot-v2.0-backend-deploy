from fastapi import APIRouter,Depends,HTTPException, File, UploadFile,Request
from fastapi import FastAPI
from ..models import Broadcast,Contacts,ChatBox,User
from ..models.User import User
from ..models.ChatBox import Last_Conversation
from ..models.ChatBox import Conversation
from ..Schemas import broadcast,user,chatbox
from ..database import database
from sqlalchemy.orm import Session
from pydantic import BaseModel
import requests
import json
from fastapi.responses import JSONResponse
import csv
import io
import pytz
from ..oauth2 import get_current_user
from dramatiq import get_broker
import asyncio
from datetime import datetime,timedelta

from ..crud.template import send_template_to_whatsapp

from fastapi import APIRouter,Depends,HTTPException, File, UploadFile,Request
from starlette.responses import PlainTextResponse
from ..oauth2 import get_current_user
from ..crud.template import send_template_to_whatsapp# Replace with your actual WhatsApp Business API endpoint and token
import logging
from apscheduler.schedulers.background import BackgroundScheduler

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from fastapi.responses import StreamingResponse
import asyncio
from typing import Generator
import time
from fastapi import APIRouter, Request, BackgroundTasks

from sqlalchemy.orm import Session
from typing import Generator
from datetime import datetime, timezone
import json
import time
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Generator
from fastapi import APIRouter, Depends,Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi.responses import StreamingResponse
from typing import AsyncGenerator
import asyncio
import json
from sqlalchemy import cast,String
from fastapi import status

# Replace with your actual WhatsApp Business API endpoint and token


router=APIRouter( tags=['Broadcast'])
app = FastAPI()

WEBHOOK_VERIFY_TOKEN = "12345"  # Replace with your verification token

# Meta Webhook verification endpoint
@router.get("/meta-webhook")
async def verify_webhook(request: Request):
    verify_token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    hubmode = request.query_params.get("hub.mode")
    print(f"Received verify_token: {challenge}, Expected: {WEBHOOK_VERIFY_TOKEN}")
    if verify_token == WEBHOOK_VERIFY_TOKEN and hubmode == "subscribe" :
        return PlainTextResponse(content=request.query_params.get("hub.challenge"),status_code=200)
    
    else:
        raise HTTPException(status_code=403, detail="Verification token mismatch")

# ######### WORKING ENDPOINT WITH BROADCAST REPORT ##########

# POST endpoint to handle webhook data from WhatsApp

@router.post("/meta-webhook")
async def receive_meta_webhook(request: Request, db: Session = Depends(database.get_db)):
    try:
        # Parse the incoming webhook request
        body = await request.json()
        print(json.dumps(body, indent=4))  # For readability of the incoming payload

        if "entry" not in body:
            raise HTTPException(status_code=400, detail="Invalid webhook format")

        # Process each entry
        for event in body["entry"]:
            if "changes" not in event:
                raise HTTPException(status_code=400, detail="Missing 'changes' key in entry")

            # Iterate through each change
            for change in event["changes"]:
                if "value" not in change:
                    raise HTTPException(status_code=400, detail="Missing 'value' key in changes")

                value = change["value"]

                # Handle messages (replies)
                if "statuses" in value:
                    for status in value["statuses"]:
                        # Check if the necessary keys exist
                        if "recipient_id" not in status or "id" not in status or "status" not in status or "timestamp" not in status:
                            raise HTTPException(status_code=400, detail="Missing keys in statuses")

                        
                        message_status = status["status"]
                        wamid=status['id']

                        message_read=False
                        message_delivered=False
                        message_sent=False

                        
                        if(message_status=="read"):
                            message_read=True
                            message_delivered=True
                            message_sent=True
                            
                        
                        if(message_status=="delivered"):
                            message_read=False
                            message_delivered=True
                            message_sent=True
                            


                        if(message_status=="sent"):
                            message_read=False
                            message_delivered=False
                            message_sent=True
                            



                        broadcast_report = (
                                db.query(Broadcast.BroadcastAnalysis)
                                .filter( Broadcast.BroadcastAnalysis.message_id==wamid)
                                .first()
                            )
                        
                        if not broadcast_report:
                                raise HTTPException(status_code=404,detail="Broadcast not found")

                        if wamid:
                                broadcast_report.read=message_read
                                broadcast_report.delivered=message_delivered
                                broadcast_report.sent=message_sent
                                broadcast_report.status=message_status

                        db.add(broadcast_report)
                        db.commit()
                        db.refresh(broadcast_report) 
                
                if "messages" in value:
                    
                    for message in value["messages"]:
                        if message.get('context', {}).get('id'):
                            message_reply=True
                            message_status='replied'
                        
                            
                            wamid=message['context']['id']
                            broadcast_report = (
                                    db.query(Broadcast.BroadcastAnalysis)
                                    .filter( Broadcast.BroadcastAnalysis.message_id==wamid)
                                    .first()
                                )
                            
                            if not broadcast_report:
                                    raise HTTPException(status_code=404,detail="Broadcast not found")

                            if wamid:
                                    broadcast_report.replied=message_sent=message_reply
                                    broadcast_report.status=message_status

                            db.add(broadcast_report)
                            db.commit()
                            db.refresh(broadcast_report)
                # Handle incoming messages and replies
                if "messages" in value:
                    await handle_incoming_messages(value, db)


        return {"message": "Webhook data received and processed successfully"}

    except KeyError as e:
        logging.error(f"Missing key in webhook payload: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Missing key: {str(e)}")
    except Exception as e:
        logging.error(f"Error processing webhook: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


async def handle_incoming_messages(value:dict, db: Session):
# Handle incoming messages
    name = value['contacts'][0]['profile']['name']


    for message in value["messages"]:
        wa_id = message['from']
        phone_number_id = value['metadata']['phone_number_id']
        message_id = message['id']
        message_content = message['text']['body']
        timestamp = int(message['timestamp'])
        message_type = message['type']
        context_message_id = message.get('context', {}).get('id')

        
        utc_time = datetime.utcfromtimestamp(timestamp)


        
        last_conversation = db.query(Last_Conversation).filter(
            Last_Conversation.sender_wa_id == wa_id,
            Last_Conversation.receiver_wa_id == phone_number_id,
            
        ).first()

        # Determine if this is the first message in a new conversation

        if last_conversation:
            # Clear previous expired conversations for this pair
            db.query(Last_Conversation).filter(
                Last_Conversation.sender_wa_id == wa_id,
                Last_Conversation.receiver_wa_id == phone_number_id
            ).delete()
            db.commit()


        last_Conversation = Last_Conversation(
                business_account_id=value['metadata'].get('business_account_id', 'unknown'),
                message_id=message_id,
                message_content=message_content,
                sender_wa_id=wa_id,
                sender_name=name,
                receiver_wa_id=phone_number_id,
                last_chat_time=utc_time,
                active=True
            )
        db.add(last_Conversation)
        db.commit()

        # Store the message in the Conversations table
        conversation = Conversation(
            wa_id=wa_id,
            message_id=message_id,
            phone_number_id=phone_number_id,
            message_content=message_content,
            timestamp=utc_time,
            context_message_id=context_message_id,
            message_type=message_type,
            direction="Receive"
        )
        db.add(conversation)
        db.commit()







@router.get("/sse/conversations/{contact_number}")
async def event_stream(contact_number: str, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(database.get_db)):
    def get_conversations() -> Generator[str, None,None]:
        last_data = None  # Track last conversation data

        while True:
            # Fetch the conversations for the given contact number
            conversations = db.query(ChatBox.Conversation).filter(ChatBox.Conversation.wa_id == contact_number).order_by(ChatBox.Conversation.timestamp).all()

            # Convert conversation instances to dictionaries
            conversation_data = [convert_to_dict(conversation) for conversation in conversations]

            # Send data only if there is a change
            if conversation_data != last_data:
                yield f"data: {json.dumps(conversation_data)}\n\n"
                last_data = conversation_data  # Update last known data

            # Check if the request has been aborted using a blocking call
            # if request.is_disconnected():
            #     break

            # Wait for a second before checking again
            time.sleep(1)

    return StreamingResponse(get_conversations(), media_type="text/event-stream")

def convert_to_dict(instance):
    """Convert SQLAlchemy model instance to a dictionary."""
    if instance is None:
        return None
    
    instance_dict = {}
    for key, value in instance.__dict__.items():
        if not key.startswith('_'):
            # Check if the value is a datetime instance
            if isinstance(value, datetime):
                instance_dict[key] = value.isoformat()  # Convert to string
            else:
                instance_dict[key] = value
    
    return instance_dict
# Assuming `ChatBox`, `database`, and `convert_to_dict` are defined elsewhere




@router.get("/active-conversations")
def get_active_conversations(
    token: str = Query(...),
    db: Session = Depends(database.get_db),  # Use Session for sync DB operations
) -> StreamingResponse:

    # Authenticate the user using the token
    current_user = get_current_user(token, db)
    if not get_current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    
    def get_active_chats() -> Generator[str, None, None]:
        last_active_chats = None  # Variable to hold the last known state of active chats
        
        while True:
            # Synchronously query the database for active chats
            active_chats = (
                db.query(ChatBox.Last_Conversation)
                .filter(cast(ChatBox.Last_Conversation.receiver_wa_id, String) == str(current_user.Phone_id))
                .all()
            )
            
            # Convert the result into a list of dictionaries
            active_chat_data = [convert_to_dict(chat) for chat in active_chats]

            # Check if the current active chats are different from the last known state
            if active_chat_data != last_active_chats:
                # Update the last known state
                last_active_chats = active_chat_data
                # Yield the updated active chats as a JSON string
                yield f"data: {json.dumps(active_chat_data)}\n\n"

            # Sleep for a while before the next check
            time.sleep(1)

    return StreamingResponse(get_active_chats(), media_type="text/event-stream")

@router.post("/send-text-message/")
def send_message(payload: chatbox.MessagePayload,db: Session = Depends(database.get_db),get_current_user: user.newuser = Depends(get_current_user)):
    # Construct the URL for sending the message
    whatsapp_url = f"https://graph.facebook.com/v20.0/{get_current_user.Phone_id}/messages"

    # Set up headers with the access token provided by the frontend
    headers = {
        "Authorization": f"Bearer {get_current_user.PAccessToken}",
        "Content-Type": "application/json"
    }

    # Construct the message payload to be sent to the WhatsApp Business API
    data = {
        "messaging_product": "whatsapp",
        "to": payload.wa_id,
        "type": "text",
        "text": {
            "body": payload.body
        }
    }

    # Send POST request to WhatsApp API
    response = requests.post(whatsapp_url, headers=headers, json=data)

    # Check for errors in the response
    if response.status_code != 200:
        print(response.json())
        raise HTTPException(status_code=response.status_code, detail=response.json())

        # return {"status": "Message sent", "response": response.json()}
    # Parse the response JSON to get message details
    response_data = response.json()
    

    try:
        # Save the sent message data in conversations table
        conversation = Conversation(
        wa_id=payload.wa_id,
        message_id=response_data.get("messages")[0].get("id"),
        phone_number_id=get_current_user.Phone_id,
        message_content=payload.body,
        timestamp=datetime.utcnow(),
        context_message_id=None,  # Set based on your needs
        message_type="text",
        direction="sent"  # Set direction to "sent"
        )
    
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        
        return {"status": "Message sent", "response": response_data}
    
    except Exception as e:
        db.rollback()  # Rollback in case of any error
        print(f"Error storing message in conversation table: {e}")
        raise HTTPException(status_code=500, detail="Error storing message in database")


@router.post("/send-template-message/")
async def send_template_message(
    request: broadcast.input_broadcast,
    get_current_user: user.newuser = Depends(get_current_user),
    db: Session = Depends(database.get_db)
):

    # Save broadcast details
    broadcastList = Broadcast.BroadcastList(
        user_id=get_current_user.id,
        name=request.name,
        template=request.template,
        contacts=[contact.phone for contact in request.recipients],  # Only storing phone numbers for now
        type=request.type,
        success=0,
        failed=0,
        status="processing..."
    )
    db.add(broadcastList)
    db.commit()
    db.refresh(broadcastList)

    saved_broadcast_id = broadcastList.id

    API_url = f"https://graph.facebook.com/v20.0/{get_current_user.Phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {get_current_user.PAccessToken}",
        "Content-Type": "application/json"
    }

    success_count = 0
    errors = []
    failed_count = 0

    for contact in request.recipients:
        recipient_name = contact.name
        recipient_phone = contact.phone
        print(recipient_name)
        data = {
            "messaging_product": "whatsapp",
            "to": recipient_phone,
            "type": "template",
            "template": {
                "name": request.template,
                "language": {"code": "en_US"},
                # You can insert recipient_name into your message template if needed
            }
        }

        if request.image_id:
            data["template"]["components"] = [
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "image",
                            "image": {"id": request.image_id}
                        }
                    ]
                }
            ]

        
        
        if request.body_parameters:
            if request.body_parameters == "Name":
                body_params = [{"type": "text","text": f"{recipient_name}"}]
                
            else:
                data["template"]["components"] = []

            if "components" not in data["template"]:
                data["template"]["components"] = []
            data["template"]["components"].append({
                "type": "body",
                "parameters": body_params
            })

        

        response = requests.post(API_url, headers=headers, data=json.dumps(data))
        response_data = response.json()

        if response.status_code == 200:
            success_count += 1
            wamid = response_data['messages'][0]['id']
            phone_num = response_data['contacts'][0]["wa_id"]

            MessageIdLog = Broadcast.BroadcastAnalysis(
                user_id=get_current_user.id,
                broadcast_id=saved_broadcast_id,
                message_id=wamid,
                status="sent",
                phone_no=phone_num,
                contact_name=recipient_name,
            )
            db.add(MessageIdLog)
            db.commit()
            db.refresh(MessageIdLog)


            # Save the sent message data in conversations table
            conversation = Conversation(
            wa_id=recipient_phone,
            message_id=response_data.get("messages")[0].get("id"),
            phone_number_id=get_current_user.Phone_id,
            message_content=f"#template_message# {request.template}",
            timestamp=datetime.utcnow(),
            context_message_id=None,  # Set based on your needs
            message_type="text",
            direction="sent"  # Set direction to "sent"
            )
        
            db.add(conversation)
            db.commit()
            db.refresh(conversation)

        else:
            failed_count += 1
            errors.append({"recipient": recipient_phone, "error": response_data})

            MessageIdLog = Broadcast.BroadcastAnalysis(
                user_id=get_current_user.id,
                broadcast_id=saved_broadcast_id,
                status="failed",
                phone_no=recipient_phone,
                contact_name=recipient_name,
            )
            db.add(MessageIdLog)
            db.commit()
            db.refresh(MessageIdLog)

    # Update broadcast status
    broadcast = db.query(Broadcast.BroadcastList).filter(Broadcast.BroadcastList.id == saved_broadcast_id).first()
    if not broadcast:
        raise HTTPException(status_code=404, detail="Broadcast not found")

    broadcast.success = success_count
    broadcast.status = "Successful" if failed_count == 0 else "Partially Successful"
    broadcast.failed = failed_count
    db.add(broadcast)
    db.commit()
    db.refresh(broadcast)

    return {
        "status": "completed",
        "successful_messages": success_count,
        "errors": errors
    }



# route for fetchlist in Broadcast template
@router.get("/templates")
def get_templates(get_current_user: user.newuser=Depends(get_current_user)):

    API_URL = f'https://graph.facebook.com/v15.0/{get_current_user.WABAID}/message_templates'
    headers = {
        'Authorization': f'Bearer {get_current_user.PAccessToken}'
    }

    response = requests.get(API_URL, headers=headers)
    
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    data = response.json()
    # Extract template names
    template_names = [template['name'] for template in data.get('data', [])]
    return JSONResponse(content=template_names)

# Route to create add broadcast in the list 
@router.post("/broadcast")
def broadcastList(request:broadcast.BroadcastListCreate,db: Session = Depends(database.get_db),get_current_user: user.newuser=Depends(get_current_user)):
    broadcastList=Broadcast.BroadcastList(
        user_id=get_current_user.id,
        name=request.name,
        template=request.template,
        contacts=request.contacts,
        type=request.type,
        success=request.success,
        failed=request.failed,
        status=request.status,
        scheduled_time=request.scheduled_time,
        task_id=request.task_id
    )
    db.add(broadcastList)
    db.commit()
    db.refresh(broadcastList)
    return{
        "broadcast_id":broadcastList.id
    } 

# Route to fetch the broadcastlist
@router.get('/broadcast')
def fetchbroadcastList(skip: int = 0, limit: int = 10, tag: str = None, db: Session = Depends(database.get_db),
                      get_current_user: user.newuser=Depends(get_current_user) ):
    broadcastList=db.query(Broadcast.BroadcastList).filter(Broadcast.BroadcastList.user_id==get_current_user.id).order_by(Broadcast.BroadcastList.id.desc()).all()
    return broadcastList
# Put route for the broadcastlist (rightnow primaryly used for updating the task_id )
@router.put("/broadcast/{broadcast_id}")
async def update_broadcast(broadcast_id: int, broadcast_update: broadcast.BroadcastListUpdate, db: Session = Depends(database.get_db),get_current_user: user.newuser=Depends(get_current_user)):
    # Retrieve the broadcast entry from the database
    broadcast = db.query(Broadcast.BroadcastList).filter(Broadcast.BroadcastList.id == broadcast_id).first()

    # If the broadcast entry does not exist, raise an HTTP 404 error
    if not broadcast:
        raise HTTPException(status_code=404, detail="Broadcast not found")

    # Update the broadcast entry with new data
    if broadcast_update.task_id:
        broadcast.task_id = broadcast_update.task_id

    # Commit the changes to the database
    db.add(broadcast)
    db.commit()
    db.refresh(broadcast)

    # Return the updated broadcast entry
    return {"message": "Broadcast updated successfully", "broadcast_id": broadcast_id, "task_id": broadcast.task_id}


# Route to fetch the scheduled broadcastlist
@router.get("/scheduled-broadcast")
def fetchScheduledbroadcastList(skip: int = 0, limit: int = 10, tag: str = None, db: Session = Depends(database.get_db),
                      get_current_user: user.newuser=Depends(get_current_user) ):
    ScheduledbroadcastList=db.query(Broadcast.BroadcastList).filter(Broadcast.BroadcastList.status=="Scheduled").order_by(Broadcast.BroadcastList.id.desc()).all()
    return ScheduledbroadcastList

# Route to CSV import contacts in the broadcast form
@router.post("/import-contacts")
def import_contacts(file: UploadFile = File(...), db: Session = Depends(database.get_db)):
    contents = file.file.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(contents))

    contacts = []
    for row in reader:
        contact = Contacts.Contact(name=row['name'], phone=row['phone'])
        contacts.append(contact)
       

    return {"contacts": contacts}
   
# Broadcast 1 routes

@router.get("/template")
def get_templates( get_current_user: user.newuser=Depends(get_current_user)):

    API_URL = f'https://graph.facebook.com/v15.0/{get_current_user.WABAID}/message_templates'
    headers = {
        'Authorization': f'Bearer {get_current_user.PAccessToken}'
    }

    response = requests.get(API_URL, headers=headers)
    
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    data = response.json()

    return data


@router.delete("/broadcasts-delete/{broadcast_id}")
async def delete_scheduled_broadcast(broadcast_id: str, db: Session = Depends(database.get_db),get_current_user: user.newuser=Depends(get_current_user)):
    # Fetch the broadcast
    broadcast = db.query(Broadcast.BroadcastList).filter(Broadcast.BroadcastList.id==broadcast_id).first()
    if not broadcast:
        raise HTTPException(status_code=404, detail="Broadcast not found")
    
    # Update the status to 'canceled'
    broadcast.status = "Cancelled"
    db.commit()
    
    return {"detail": "Scheduled broadcast has been canceled."}



import logging

@router.post("/create-template", response_model=broadcast.TemplateResponse)
async def create_template(template: broadcast.TemplateCreate , request : Request , get_current_user: user.newuser=Depends(get_current_user)):
    try:
        template = await request.json()
        broadcast.TemplateCreate.validate_template(template)
        response = await send_template_to_whatsapp(template , get_current_user.PAccessToken , get_current_user.WABAID )
        return response
    except HTTPException as e:
        logging.critical(e)
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    

@router.get("/broadcast-report/{broadcast_id}")
def BroadcastReport(broadcast_id:int,get_current_user: user.newuser=Depends(get_current_user),db: Session = Depends(database.get_db)):

    broadcast_data=db.query(Broadcast.BroadcastAnalysis).filter((Broadcast.BroadcastAnalysis.user_id==get_current_user.id) &(Broadcast.BroadcastAnalysis.broadcast_id==broadcast_id)).all()

    if not broadcast_data:
        raise HTTPException(status_code=404,detail="Broadcast data not found")

    return broadcast_data

@router.post("/upload-media")
async def upload_file(file: UploadFile = File(...),get_current_user: user.newuser=Depends(get_current_user)):
    # Read the contents of the uploaded file
    contents = await file.read()
    

    # Upload the file to WhatsApp directly from memory (no need to save it)
    try:
        media_url = f"https://graph.facebook.com/v20.0/{get_current_user.Phone_id}/media"
        headers = {
            "Authorization": f"Bearer {get_current_user.PAccessToken}"
        }
        files = {
            'file': (file.filename, contents, file.content_type),  # File from memory
        }
        data = {
            'type': file.content_type.split("/")[0],
            'messaging_product': 'whatsapp' # Extract media type (image, video, etc.)
        }

        # Make the POST request to upload media to WhatsApp
        response = requests.post(media_url, headers=headers, files=files, data=data)

        # Check for errors in the WhatsApp API response
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.json())

        # Get the media ID from the response
        media_id = response.json().get("id")

        return JSONResponse(content={
            "filename": file.filename,
            "file_size": len(contents),
            "content_type": file.content_type,
            "whatsapp_media_id": media_id
        })
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error uploading file to WhatsApp: {str(e)}")


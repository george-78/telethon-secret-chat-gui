#pip install mutagen, moviepy
from PyQt5.QtCore import QObject, QRunnable, pyqtSignal
import asyncio
from telethon import TelegramClient, events
from telethon_secret_chat import SecretChatManager
from telethon_secret_chat.secret_sechma.secretTL import DecryptedMessageService, DecryptedMessageMediaPhoto, \
    DecryptedMessageMediaVideo, DecryptedMessageMediaExternalDocument, DocumentAttributeSticker, \
    DecryptedMessageActionReadMessages, DecryptedMessageActionDeleteMessages
from telethon.tl.types import MessageEntityBold, MessageEntityItalic, InputEncryptedChat
from telethon.tl.functions.messages import SendEncryptedServiceRequest
from PIL import Image
from mutagen import File as MutagenFile
from moviepy import VideoFileClip
import io
import os
import re
import mimetypes


api_id = ''
api_hash = ''
username = "@"
TTL = 0 # default self-destruct timeout

def classify_file_type(file_path):
    mime_type, _ = mimetypes.guess_type(file_path)

    if not mime_type is None:
        if mime_type.startswith('audio'):
            return 'audio'
        if mime_type.startswith('video'):
            return 'video'
        if mime_type.startswith('image'):
            return 'photo'
    
    return 'other'
    

def telegram_markup_to_html(text):
    # Convert bold: **bold**
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    
    # Convert italic: __italic__
    text = re.sub(r'__(.+?)__', r'<i>\1</i>', text)

    # Optional: replace line breaks with <br> (Telegram messages often use \n)
    text = text.replace('\n', '<br>')

    return text


def prepare_secret_video_data(video_path):
    # Load video
    clip = VideoFileClip(video_path)
    
    # Duration in seconds (rounded)
    duration = int(clip.duration)
    
    # Width and height
    w, h = clip.size

    # Extract frame (thumbnail) at 1 second or 0 if video is too short
    frame_time = min(1, clip.duration - 0.1)
    frame = clip.get_frame(frame_time)
    
    # Convert numpy frame to PIL Image
    image = Image.fromarray(frame)
    
    # Resize thumbnail if needed
    thumb_size = (90, 90)
    image.thumbnail(thumb_size)
    thumb_w, thumb_h = image.size
    
    # Save to bytes
    thumb_io = io.BytesIO()
    image.save(thumb_io, format='JPEG')
    thumb_bytes = thumb_io.getvalue()

    # File size in bytes
    size = os.path.getsize(video_path)

    # Detect MIME type
    mime_type, _ = mimetypes.guess_type(video_path)
    if mime_type is None:
        mime_type = "video/mp4"  # Default fallback

    return {
        "video": video_path,
        "thumb": thumb_bytes,
        "thumb_w": thumb_w,
        "thumb_h": thumb_h,
        "duration": duration,
        "mime_type": mime_type,
        "w": w,
        "h": h,
        "size": size
    }


def prepare_secret_audio_data(audio_path):
    # File size
    size = os.path.getsize(audio_path)

    # MIME type
    mime_type, _ = mimetypes.guess_type(audio_path)
    if not mime_type:
        mime_type = "application/octet-stream"  # fallback

    # Duration
    audio = MutagenFile(audio_path)
    duration = int(audio.info.length) if audio and audio.info else 0

    return {
        "audio": audio_path,
        "duration": duration,
        "mime_type": mime_type,
        "size": size
    }


def prepare_secret_photo_data(filename):
    mime_type, _ = mimetypes.guess_type(filename)
    if mime_type is None:
        mime_type = "image/png"  # Default fallback

    # Open the image
    with Image.open(filename) as img:
        # Get width and height
        width, height = img.size

        # Create thumbnail with proportional resizing (max 90px on longest side)
        if width > height:
            thumb_w = 90
            thumb_h = int(90 * height / width)
        elif height > width:
            thumb_w = int(90 * width / height)
            thumb_h = 90
        else:
            thumb_w = thumb_h = 90

        # Generate thumbnail
        thumb_size = (thumb_w, thumb_h)
        img_thumb = img.copy()
        img_thumb.thumbnail(thumb_size)

        # Save thumbnail to bytes
        thumb_bytes_io = io.BytesIO()
        img_thumb.save(thumb_bytes_io, format='PNG')
        thumb_bytes = thumb_bytes_io.getvalue()
        thumb_w, thumb_h = img_thumb.size

    # Get file size
    size = os.path.getsize(filename)

    return {
        "thumb": thumb_bytes,
        "thumb_w": thumb_w,
        "thumb_h": thumb_h,
        "w": width,
        "h": height,
        "size": size,
        "mime_type": mime_type
    }


def to_html(message, entities):
    html = list(message)
    insertions = []

    if entities:
        for entity in entities:
            start = entity.offset
            end = entity.offset + entity.length

            if isinstance(entity, MessageEntityBold):
                insertions.append((start, "<b>"))
                insertions.append((end, "</b>"))
            elif isinstance(entity, MessageEntityItalic):
                insertions.append((start, "<i>"))
                insertions.append((end, "</i>"))
            # You can extend this with more entity types if needed.

    # Sort by position in reverse so insertions don't shift other insertions
    for pos, tag in sorted(insertions, key=lambda x: -x[0]):
        html.insert(pos, tag)

    return ''.join(html).replace('\n', '<br>')


class TelegramWorkerSignals(QObject):
    message_received = pyqtSignal(str)

class TelegramWorker(QRunnable):
    def __init__(self, api_id, api_hash, username, queue):
        super().__init__()
        self.api_id = api_id
        self.api_hash = api_hash
        self.username = username
        self.signals = TelegramWorkerSignals()
        self.queue = queue  # asyncio.Queue for incoming messages
        self.loop = None


    def run(self):
        asyncio.run(self.main())


    async def main(self):
        self.loop = asyncio.get_event_loop()
        async with TelegramClient("session_name2", self.api_id, self.api_hash) as client:
            self.client = client

            self.manager = SecretChatManager(
                client,
                session=client.session,
                auto_accept=True,
                new_chat_created=self.new_chat
            )
            self.manager.add_secret_event_handler(func=self.replier)

            self.chat = await self.manager.start_secret_chat(username)
            print("Started secret chat:", self.chat)

            await self.start_chat_loop()


    async def replier(self, event):
        global TTL
        print("Received event:", event)
        if event.decrypted_event.message:
            message_text = to_html(event.decrypted_event.message, event.decrypted_event.entities)
            self.signals.message_received.emit(f"[TG] {message_text}")
        
        TTL = event.decrypted_event.ttl

        peer = self.manager.get_secret_chat(self.chat)
        message = DecryptedMessageService(action=DecryptedMessageActionReadMessages([event.decrypted_event.random_id]))
        message = await self.manager.encrypt_secret_message(peer, message)
        await self.client(SendEncryptedServiceRequest(InputEncryptedChat(peer.id, peer.access_hash), message))
        
        if event.decrypted_event.media is None:
            return

        if isinstance(event.decrypted_event.media, DecryptedMessageMediaExternalDocument):
            for attr in event.decrypted_event.media.attributes:
                if isinstance(attr, DocumentAttributeSticker):
                    self.signals.message_received.emit(f"[TG] {attr.alt}")                    
                    break
            
            return

        if isinstance(event.decrypted_event.media, DecryptedMessageMediaPhoto):
            file_name = str(event.decrypted_event.random_id) + '.jpg'
        elif isinstance(event.decrypted_event.media, DecryptedMessageMediaVideo):
            file_name = str(event.decrypted_event.random_id) + '.mp4'
        else:
            file_name = str(event.decrypted_event.random_id) + '.unknown' 

        print(f"Processing {file_name}")
       
        cache = await self.manager.download_secret_media(event.decrypted_event)

        file_path = f"{file_name}"
        with open(file_path, "wb") as f:
            f.write(cache)
            f.close()
        os.startfile(file_path)
        

    async def new_chat(self, chat, created_by_me):
        print("New secret chat created:", chat, "by me?" , created_by_me)


    async def start_chat_loop(self):
        while True:
            # Wait for messages from the UI to send via Telegram
            message = await self.queue.get()
            await self.send_message(message)


    async def send_message(self, message):
        global TTL

        # Customize this to send message to a specific chat
        if os.path.isfile(message):
            file_path = message
            file_type = classify_file_type(file_path)
            if file_type == 'audio':
                data = prepare_secret_audio_data(file_path)
                await self.manager.send_secret_audio(self.chat, file_path, 
                    data["duration"], data["mime_type"], data["size"])
                
            elif file_type == 'video':
                data = prepare_secret_video_data(file_path)
                await self.manager.send_secret_video(self.chat, file_path, 
                    data["thumb"], data["thumb_w"], data["thumb_h"], data["duration"], data["mime_type"], data["w"], data["h"], data["size"])
                
            elif file_type == 'photo':
                data = prepare_secret_photo_data(file_path)
                #await self.manager.send_secret_document(self.chat, file_path, data["thumb"], data["thumb_w"], data["thumb_h"], "somep.png", data["mime_type"], data["size"])
                await self.manager.send_secret_photo(self.chat, file_path, 
                    data["thumb"], data["thumb_w"], data["thumb_h"], data["w"], data["h"], data["size"])
                
            else:
                # I have no clue on this one yet
                await self.manager.send_secret_document(self.chat, file_path, None, None, None, None, None, None)
        else:
            res = await self.manager.send_secret_message(self.chat, message, ttl = TTL)
            print(res)


from PyQt5.QtWidgets import QTextEdit, QApplication, QWidget, QVBoxLayout, QPushButton, QLineEdit, QFileDialog, QMessageBox
from PyQt5.QtCore import QThreadPool, Qt
import asyncio

class CustomTextEdit(QTextEdit):
    def __init__(self, send_callback, parent=None):
        super().__init__(parent)
        self.send_callback = send_callback

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() == Qt.ShiftModifier:
                # Shift+Enter: insert newline
                self.insertPlainText('\n')
            else:
                # Enter only: trigger send
                self.send_callback()
        else:
            super().keyPressEvent(event)


class QAApp(QWidget):
    def __init__(self, api_id, api_hash, username):
        super().__init__()
        self.setWindowTitle("Telegram Chat App")
        self.layout = QVBoxLayout(self)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.input = CustomTextEdit(self.handle_send)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self.handle_send)

        self.layout.addWidget(self.output)
        self.layout.addWidget(self.input)
        self.layout.addWidget(self.send_button)

        # Text field to display the file path
        self.file_path_input = QLineEdit(self)
        self.file_path_input.setPlaceholderText("Select a file...")
        self.layout.addWidget(self.file_path_input)

        # Button to browse files
        self.browse_button = QPushButton("Browse", self)
        self.browse_button.clicked.connect(self.browse_file)
        self.layout.addWidget(self.browse_button)

        # Upload button
        self.upload_button = QPushButton("Upload", self)
        self.upload_button.clicked.connect(self.upload_file)
        self.layout.addWidget(self.upload_button)

        self.queue = asyncio.Queue()
        self.worker = TelegramWorker(api_id, api_hash, username, self.queue)
        self.worker.signals.message_received.connect(self.display_message)

        QThreadPool.globalInstance().start(self.worker)
        

    def browse_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select File")
        if file_path:
            self.file_path_input.setText(file_path)


    def upload_file(self):
        file_path = self.file_path_input.text()
        if file_path:
            asyncio.create_task(self.queue.put(file_path))
        else:
            QMessageBox.warning(self, "Error", "Please select a file first!")


    def handle_send(self):
        message = self.input.toPlainText().strip()
        if message:
            self.input.clear()
            asyncio.create_task(self.queue.put(message))
            html_message = telegram_markup_to_html(message)
            self.display_message(f"[Me] {html_message}")


    def display_message(self, message):
        self.output.append(message)

import sys
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop

if __name__ == '__main__':
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = QAApp(api_id, api_hash, username)
    window.show()

    with loop:
        loop.run_forever()

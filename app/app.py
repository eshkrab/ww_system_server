import os
import json
import zmq
import zmq.asyncio
from queue import Queue
from quart import Quart, websocket, send_file, request, jsonify
from quart_cors import cors, route_cors

from werkzeug.utils import secure_filename

import aiofiles
import asyncio

app = Quart(__name__)
app = cors(app, allow_origin="*")

video_dir = "../../content/"
ws_queue = Queue(maxsize=10)

ALLOWED_EXTENSIONS = {"mp4", "avi", "mov"}

ctx = zmq.asyncio.Context()
socket = ctx.socket(zmq.REQ)  # REQ type socket for ZMQ
socket.connect("tcp://localhost:5555")  # Connect to the player app

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

async def send_message_to_player(message):
    await socket.send_string(message)
    reply = await socket.recv_string()  # Waiting for reply
    return reply

# Content player REST API
@app.route("/api/state", methods=["GET", "POST"])
async def set_state():
    if request.method == "POST":
        state = request.form.get("state")
        reply = await send_message_to_player(state)
        print(f"Reply from player: {reply}")
        return jsonify({"success": True, "reply": reply})

@app.route("/api/playlist", methods=["GET", "POST"])
async def handle_playlist():
    if request.method == "GET":
        reply = await send_message_to_player("get_playlist")
        return jsonify({"playlist": reply})


@app.route("/api/brightness", methods=["GET", "POST"])
@route_cors(allow_origin="*")
async def handle_brightness():
    if request.method == "POST":
        brightness = request.form.get("brightness")
        reply = await send_message_to_player(f"set_brightness {brightness}")
        return jsonify({"success": True, "reply": reply})


@app.route('/api/fps', methods=['GET', 'POST'])
@route_cors(allow_origin="*")
async def set_fps():
    # Insert logic to adjust FPS
    return 'FPS adjusted'

@app.route("/api/videos", methods=['GET', 'POST', 'DELETE'])
@route_cors(allow_origin="*")
async def handle_videos():
    if request.method == "GET":
        videos = [
            f
            for f in os.listdir(video_dir)
            if os.path.isfile(os.path.join(video_dir, f)) and allowed_file(f)
        ]
        return jsonify({"videos": videos})

    elif request.method == "POST":
        if "file" not in (await request.files):
            return jsonify({"error": "No file part"}), 400
        file = (await request.files)["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            await file.save(os.path.join(video_dir, filename))
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Unsupported file type"}), 400

    elif request.method == "DELETE":
        filename = (await request.form).get("filename")
        if filename is None:
            return jsonify({"error": "Filename is missing"}), 400
        file_path = os.path.join(video_dir, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            return jsonify({"success": True})
        else:
            return jsonify({"error": "File not found"}), 404

    else:
        return jsonify({"error": "Unsupported method"}), 405



@app.route("/api/videos", methods=['GET', 'POST', 'DELETE'])
@route_cors(allow_origin="*")
def handle_videos():
    if request.method == "GET":
        videos = [
            f
            for f in os.listdir(video_dir)
            if os.path.isfile(os.path.join(video_dir, f)) and allowed_file(f)
        ]
        return jsonify({"videos": videos})

    elif request.method == "POST":
        if "file" not in request.files:
            return jsonify({"error": "No file part"}), 400
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(video_dir, filename))
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Unsupported file type"}), 400

    elif request.method == "DELETE":
        filename = request.form.get("filename")
        if filename is None:
            return jsonify({"error": "Filename is missing"}), 400
        file_path = os.path.join(video_dir, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            return jsonify({"success": True})
        else:
            return jsonify({"error": "File not found"}), 404

    else:
        return jsonify({"error": "Unsopported method"}), 404


# WebSocket endpoint for streaming video frames
@app.websocket('/stream')
async def stream():
    async with aiofiles.open('video.mp4', mode='rb') as f:
        while True:
            data = await f.read(1024)  # Read video file in chunks of 1024 bytes
            if not data:
                break
            await websocket.send(data)
            await asyncio.sleep(0.1)  # Adjust this to control the streaming speed

if __name__ == '__main__':
    app.run(host = "0.0.0.0", port = 5000)


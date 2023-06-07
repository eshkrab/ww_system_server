import os
import json
import zmq
import zmq.asyncio
from queue import Queue
from quart import Quart, websocket, send_file, request, jsonify
from quart_cors import cors, route_cors

#thumbnail
from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
from moviepy.editor import VideoFileClip
import hashlib


from werkzeug.utils import secure_filename

import aiofiles
import asyncio

import logging

app = Quart(__name__)
#  app = cors(app, allow_origin="*")
app = cors(app, allow_origin="*", allow_headers="*", allow_methods="*")


def load_config(config_file):
    with open(config_file, 'r') as f:
        config = json.load(f)
    return config

def get_log_level( level):
    levels = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    return levels.get(level.upper(), logging.INFO)

config = load_config('config/config.json')

video_dir = config['video_dir']
logging.basicConfig(level=get_log_level(config['debug']['log_level']))

ALLOWED_EXTENSIONS = {"mp4", "avi", "mov"}

ctx = zmq.asyncio.Context()
socket = ctx.socket(zmq.REQ)

socket.connect(f"tcp://{config['zmq']['ip_server']}:{config['zmq']['port']}")  # Connect to the player app
#  socket.connect("tcp://player:5555")

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

zmq_lock = asyncio.Lock()

async def send_message_to_player(message):
    async with zmq_lock:
        try:
            logging.info(f"Sending message: {message}")
            await socket.send_string(message)
            reply = await socket.recv_string()
            return reply
        except zmq.ZMQError as e:
            logging.error(f"ZMQError while sending/receiving message: {e}")
            return -1
#  def send_message_to_player(message):
#      try:
#          logging.info(f"Sending message: {message}")
#          socket.send_string(message)
#          reply = socket.recv_string()
#          return reply
#      except zmq.ZMQError as e:
#          logging.error(f"Encountered ZMQError while sending/receiving message: {e}")
#          return None


def generate_thumbnail_path(video_filename):
    video_path = os.path.join(video_dir, video_filename)
    thumbnail_filename = f"{video_filename}_thumbnail.jpg"
    thumbnail_path = os.path.join(video_dir, thumbnail_filename)

    if not os.path.exists(thumbnail_path):
        # Load video file
        clip = VideoFileClip(video_path)

        # Find the middle frame of the video
        midpoint = clip.duration / 2

        # Save the thumbnail image
        clip.save_frame(thumbnail_path, t=midpoint)

    return thumbnail_path


@app.route("/api/state", methods=["GET", "POST"])
@route_cors(allow_origin="*")
async def set_state():
    if request.method == "POST":
        logging.debug('Received a POST STATE request')
        form_data = await request.form
        state = form_data.get("state")

        if state is None:
            logging.error("Error: state post request is None")
            return jsonify({"error": "State is None"}), 400

        reply = await send_message_to_player(state)
        #  if reply is None:
        #      app.logger.error(f"Error POST STATE response: {reply}")
        #      return jsonify({"error": "An error occurred while communicating with the player"}), 500

        app.logger.debug(f" POST STATE response: {reply}")
        return jsonify({"success": True, "reply": reply})

    if request.method == "GET":
        logging.debug('Received a GET STATE request')
        #  state = "playing"
        state = await send_message_to_player("get_state")
        #  #  if state is None:
        #  #      app.logger.error(f"Error POST state response is NONE")
        #  #      return jsonify({"error": "An error occurred while communicating with the player"}), 500
        app.logger.debug(f" GET STATE from player response: {state}")
        return jsonify({"success": True, "state": state})

    logging.error("STATE Error: Invalid request method")
    return jsonify({"error": "Invalid request method"}), 405

@app.route("/api/mode", methods=["GET", "POST"])
@route_cors(allow_origin="*")
async def set_mode():
    if request.method == "POST":
        logging.info('Received a POST request')
        form_data = await request.form
        mode = form_data.get("mode")

        if mode is None:
            logging.error("Error: mode is None")
            return jsonify({"error": "mode is None"}), 400

        reply = await send_message_to_player(mode.upper())
        logging.info(f"GET MODE Reply from player: {reply}")
        return jsonify({"success": True, "reply": reply})

    if request.method == "GET":
        #  logging.debug('Received a GET request')
        mode = await send_message_to_player("get_mode")
        if mode is None:
            app.logger.error(f"Error POST state response is NONE")
            return jsonify({"error": "An error occurred while communicating with the player"}), 500
        app.logger.debug(f" GET state response: {mode}")
        return jsonify({"success": True, "mode": mode, "reply": mode})

    logging.error("MODE Error: Invalid request method")
    return jsonify({"error": "Invalid request method"}), 405

@app.route("/api/playlist", methods=["GET", "POST"])
@route_cors(allow_origin="*")
async def handle_playlist():
    playlist_path = os.path.join(video_dir, "playlist.json")
    if request.method == "GET":
        async with aiofiles.open(playlist_path, "r") as f:
            playlist = json.loads(await f.read())
        return jsonify(playlist)
    elif request.method == "POST":
        playlist = await request.get_json()
        async with aiofiles.open(playlist_path, "w") as f:
            await f.write(json.dumps(playlist))
        await send_message_to_player("set_playlist")
        return jsonify({"success": True})

    logging.error("PLAYLIST Error: Invalid request method")
    return jsonify({"error": "Invalid request method"}), 405


@app.route("/api/brightness", methods=["GET", "POST"])
@route_cors(allow_origin="*")
async def handle_brightness():
    if request.method == "POST":
        logging.info('Received a POST BRIGHTNESS request')
        form_data = await request.form
        brightness = float(form_data.get("brightness"))
        brightness = int(brightness/100.0 * 255.0)
        reply = await send_message_to_player(f"set_brightness {brightness}")
        logging.debug(f"Brightness from player: {reply}")
        return jsonify({"success": True, "reply": reply})

    if request.method == "GET":
        logging.info('Received a GET BRIGHTNESS request')
        brightness = await send_message_to_player("get_brightness")
        brightness = float(brightness) / 255.0  
        app.logger.debug(f" GET Brightness response: {brightness}")
        return jsonify({"success": True, "brightness": brightness})

    logging.error("BRIGHTNESS Error: Invalid request method")
    return jsonify({"error": "Invalid request method"}), 405

@app.route('/api/fps', methods=['GET', 'POST'])
@route_cors(allow_origin="*")
async def set_fps():
    if request.method == "GET":
        fps = await send_message_to_player("get_fps")
        return jsonify({"success": True, "fps": fps})
    if request.method == "POST":
        form_data = await request.form
        fps = int(float(form_data.get("fps")))
        reply = await send_message_to_player(f"set_fps {fps}")
        return jsonify({"success": True, "reply": reply})

    logging.error("FPS Error: Invalid request method")
    return jsonify({"error": "Invalid request method"}), 405

@app.route("/api/videos", methods=['GET', 'POST', 'DELETE'])
@route_cors(allow_origin="*")
async def handle_videos():
    if request.method == "GET":
        videos = [
            {
                "name": f,
                "filepath": os.path.join(video_dir, f),
                "thumbnail": generate_thumbnail_path(f)  # Assuming you have a function to generate thumbnail paths
            }
            for f in os.listdir(video_dir)
            if os.path.isfile(os.path.join(video_dir, f)) and allowed_file(f)
        ]
        return jsonify({"mediaFiles": videos})

    if request.method == "POST":
        file = (await request.files).get("file")
        if not file or file.filename == "":
            return jsonify({"error": "No file selected or file name is empty"}), 400
        if allowed_file(file.filename):
            filename = secure_filename(file.filename)
            await file.save(os.path.join(video_dir, filename))
            #  generate_thumbnail_path(filename)
            logging.info(f"File {filename} saved")
            return jsonify({"success": True})
        return jsonify({"error": "Unsupported file type"}), 400

    if request.method == "DELETE":
        filename = (await request.form).get("filename")
        if not filename:
            return jsonify({"error": "Filename is missing"}), 400
        file_path = os.path.join(video_dir, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            return jsonify({"success": True})
        return jsonify({"error": "File not found"}), 404

    return jsonify({"error": "Unsupported method"}), 405

@app.websocket('/stream')
@route_cors(allow_origin="*")
async def stream():
    async with aiofiles.open('video.mp4', mode='rb') as f:
        while True:
            data = await f.read(1024)
            if not data:
                break
            await websocket.send(data)
            await asyncio.sleep(0.1)

if __name__ == '__main__':
    app.run(host = f"{config['rest_api']['ip']}", port = int(config['rest_api']['port']))


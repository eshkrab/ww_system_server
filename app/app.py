import os
import json
import time
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

class Player:
    def __init__(self, brightness=250.0, fps=30, state="stopped", mode="repeat", current_media=None):
        self.state = state
        self.mode = mode
        self.brightness = brightness
        self.fps = fps
        self.current_media = current_media

app = Quart(__name__)
#  app = cors(app, allow_origin="*")
app = cors(app, allow_origin="*", allow_headers="*", allow_methods="*")

############################
# CONFIG
############################
def load_config(config_file):
    with open(config_file, 'r') as f:
        config = json.load(f)
    return config

def save_config(config, config_file):
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=4)

def get_log_level( level):
    levels = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    return levels.get(level.upper(), logging.INFO)

config_path = 'config/config.json'
config = load_config(config_path)

logging.basicConfig(level=get_log_level(config['debug']['log_level']))

video_dir = config['video_dir']

############################
# PLAYER
############################
player = Player(brightness=config['brightness_level'], fps=config['fps'])

ALLOWED_EXTENSIONS = config['video_ext']
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_thumbnail_path(video_filename):
    video_filename = os.path.basename(video_filename)
    thumbnail_filename = f"{os.path.splitext(video_filename)[0]}_thumbnail.jpg"
    thumbnail_path = os.path.join(video_dir, thumbnail_filename)

    video_path = os.path.join(video_dir, video_filename)
    if not os.path.exists(thumbnail_path):
        if "ww" in ALLOWED_EXTENSIONS:
            return "no thumbnails for ww"
        # Load video file
        clip = VideoFileClip(video_path)

        # Find the middle frame of the video
        midpoint = clip.duration / 2

        # Save the thumbnail image
        clip.save_frame(thumbnail_path, t=midpoint)

    return thumbnail_path

########################
# ZMQ
########################
ctx = zmq.asyncio.Context()
# Publish to the player app
pub_socket = ctx.socket(zmq.PUB)
pub_socket.bind(f"tcp://{config['zmq']['ip_bind']}:{config['zmq']['port_server_pub']}")  # Publish to the player app

# Subscribe to the player app
sub_socket = ctx.socket(zmq.SUB)
sub_socket.connect(f"tcp://{config['zmq']['ip_connect']}:{config['zmq']['port_player_pub']}")  
sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")

def reset_socket(sub_socket):
    logging.debug("Resetting socket")
    # close the current socket
    sub_socket.close()
    # create a new socket
    new_sock = ctx.socket(zmq.SUB)
    logging.debug(f"Subscribing to tcp://{config['zmq']['ip_connect']}:{config['zmq']['port_player_pub']}")

    # connect the new socket
    try:
        logging.debug(f"OPENING UP SOCKET AGAIN to tcp://{config['zmq']['ip_connect']}:{config['zmq']['port_player_pub']}")
        new_sock.connect(f"tcp://{config['zmq']['ip_connect']}:{config['zmq']['port_player_pub']}")  
        new_sock.setsockopt_string(zmq.SUBSCRIBE, "")
    except zmq.ZMQError as zmq_error:
        logging.error(f"Subscribing to tcp://{config['zmq']['ip_connect']}:{config['zmq']['port_player_pub']}")
        logging.error(f"ZMQ Error occurred during socket reset: {str(zmq_error)}")
    return new_sock

LAST_MSG_TIME = time.time()

async def monitor_socket():
    #monitor sub_socket and if it's been too long since LAST_MSG_TIME, reset the socket
    global sub_socket
    global LAST_MSG_TIME
    logging.debug("Monitoring socket")

    while True:

        #  logging.debug(f"Time since last message: {time.time() - LAST_MSG_TIME}")
        # Check if it's been 1 minute since last message received
        if time.time() - LAST_MSG_TIME > 10:
            logging.debug("Resetting socket")
            fut = asyncio.ensure_future(sub_socket.recv())
            try:
                resp = await asyncio.wait_for(fut, timeout=0.5)  # Close the previous socket only after a short time-out
                LAST_MSG_TIME = time.time()
                logging.debug("New message received, not resetting the socket!")
            except asyncio.TimeoutError:
                sub_socket = reset_socket(sub_socket)
                LAST_MSG_TIME = time.time()

        await asyncio.sleep(1)

async def send_message_to_player(message):
    try:
        logging.debug(f"Publishing message: {message}")
        await pub_socket.send_string(message)
    except zmq.ZMQError as e:
        logging.error(f"ZMQError while publishing message: {e}")
        return -1

async def subscribe_to_player():
    global sub_socket
    global LAST_MSG_TIME

    # monitor unsaved changes
    last_change_at = time.time()
    unsaved_changes = False

    logging.debug("SUBSCRIBED to player")

    while True:
        message = await sub_socket.recv_string()
        LAST_MSG_TIME = time.time()

        # Process the received message
        message = message.split(" ")
        if message[0] == "state":
            player.state = message[1]
        elif message[0] == "mode":
            player.mode = message[1]
        elif message[0] == "brightness":
            brightness = float(message[1]) / 255.0
            player.brightness = float(brightness)
            last_change_at = time.time()
            unsaved_changes = True
        elif message[0] == "fps":
            player.fps = int(message[1])
            last_change_at = time.time()
            unsaved_changes = True
        elif message[0] == "current_media":
            player.current_media = message[1]
        else:
            logging.error(f"Unknown message from Player: {message}")

        if time.time() - last_change_at > 60.0 and unsaved_changes:
            config['brightness_level'] = player.brightness
            config['fps'] = player.fps
            save_config(config, config_path)
            unsaved_changes = False

        await asyncio.sleep(0.1)

        
            


#####################################################
# API
#####################################################

@app.before_serving
async def startup():
    global zmq_lock
    zmq_lock = asyncio.Lock()

    asyncio.create_task(subscribe_to_player()) 
    #  asyncio.create_task(monitor_socket())
    logging.debug("Subscribed to player")

@app.route("/api/state", methods=["GET", "POST"])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def set_state():
    if request.method == "POST":
        logging.debug('Received a POST STATE request')
        form_data = await request.form
        state = form_data.get("state")

        if state is None:
            logging.error("Error: state post request is None")
            return jsonify({"error": "State is None"}), 400

        await send_message_to_player(state)
        return jsonify({"success": True, "reply": "OK"})

    if request.method == "GET":
        app.logger.debug(f" GET STATE from player: {player.state}")
        return jsonify({"success": True, "state": player.state})

    logging.error("STATE Error: Invalid request method")
    return jsonify({"error": "Invalid request method"}), 405

@app.route("/api/mode", methods=["GET", "POST"])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def set_mode():
    if request.method == "POST":
        logging.debug('Received a POST request')
        form_data = await request.form
        mode = form_data.get("mode")

        if mode is None:
            logging.error("Error: mode is None")
            return jsonify({"error": "mode is None"}), 400

        await send_message_to_player(mode.upper())
        return jsonify({"success": True, "reply": "OK"})

    if request.method == "GET":
        app.logger.debug(f" GET state response: {player.mode}")
        return jsonify({"success": True, "mode": player.mode})

    logging.error("MODE Error: Invalid request method")
    return jsonify({"error": "Invalid request method"}), 405

@app.route("/api/playlist", methods=["GET", "POST"])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def handle_playlist():
    playlist_path = os.path.join(video_dir, "playlist.json")

    if request.method == "GET":
        async with aiofiles.open(playlist_path, "r") as f:
            playlist = json.loads(await f.read())
        logging.debug(f"GET PLAYLIST response: {playlist}")
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
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def handle_brightness():
    if request.method == "POST":
        #  logging.debug('Received a POST BRIGHTNESS request')
        form_data = await request.form
        brightness = float(form_data.get("brightness"))
        brightness = int(brightness/100.0 * 255.0)
        await send_message_to_player(f"set_brightness {brightness}")
        return jsonify({"success": True, "reply": "OK"})

    if request.method == "GET":
        app.logger.debug(f" GET Brightness response: {player.brightness}")
        return jsonify({"success": True, "brightness": player.brightness})

    logging.error("BRIGHTNESS Error: Invalid request method")
    return jsonify({"error": "Invalid request method"}), 405

@app.route('/api/fps', methods=['GET', 'POST'])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def set_fps():
    if request.method == "GET":
        #  logging.debug('Received a GET FPS request')
        logging.debug(f"FPS from player: {player.fps}")
        return jsonify({"success": True, "fps": float(player.fps)})

    if request.method == "POST":
        form_data = await request.form
        fps = int(float(form_data.get("fps")))
        await send_message_to_player(f"set_fps {fps}")
        return jsonify({"success": True, "reply": "OK"})

    logging.error("FPS Error: Invalid request method")
    return jsonify({"error": "Invalid request method"}), 405

@app.route("/api/videos", methods=['GET', 'POST', 'DELETE'])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
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
            logging.debug(f"File {filename} saved")
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


@app.route("/api/currentMedia", methods=["GET"])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def get_current_media():

    current_media = player.current_media
    if current_media is None:
        app.logger.error("Error getting current media response")
        return jsonify({"error": "An error occurred while communicating with the player"}), 500

    elif type(current_media) == str:
        video_file = {
            "name": current_media,
            "filepath": os.path.join(video_dir, current_media),
            "thumbnail": generate_thumbnail_path(current_media)
        }
        logging.debug(f"GET CURRENT MEDIA response: {video_file}")
        return jsonify(video_file)

@app.route("/thumbnails/<filename>")
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def serve_thumbnail(filename):
    logging.debug(f"Thumbnail request for {filename}")
    thumbnail_path = generate_thumbnail_path(filename)
    logging.debug(f"Thumbnail path: {thumbnail_path}")
    return await send_file(thumbnail_path)

@app.websocket('/stream')
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def stream():
    async with aiofiles.open('content/box_test.mov', mode='rb') as f:
        while True:
            data = await f.read(1024)
            if not data:
                break
            await websocket.send(data)
            await asyncio.sleep(0.1)


if __name__ == '__main__':
    #  loop = asyncio.get_event_loop()
    #  loop.create_task(subscribe_to_player())
    #  loop.create_task(monitor_socket())

    #  # ZMQ socket
    #  logging.debug("Subscribed to player")

    app.run(host = f"{config['rest_api']['ip']}", port = int(config['rest_api']['port']))


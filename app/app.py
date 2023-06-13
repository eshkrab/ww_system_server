import os
import json
import zmq.asyncio
from quart import Quart, websocket, send_from_directory, request, jsonify
from quart_cors import cors, route_cors
from werkzeug.utils import secure_filename
from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
from moviepy.editor import VideoFileClip
from cerberus import Validator
import aiofiles
import asyncio
import logging

app = Quart(__name__)
app = cors(app, allow_origin="*", allow_headers="*", allow_methods="*")

async def load_config(config_file):
    async with aiofiles.open(config_file, 'r') as f:
        config = json.loads(await f.read())
    return config

def get_log_level(level):
    levels = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    return levels.get(level.upper(), logging.INFO)

config = asyncio.run(load_config('config/config.json'))

video_dir = config['video_dir']
logging.basicConfig(level=get_log_level(config['debug']['log_level']))

ALLOWED_EXTENSIONS = set(config['video_ext'])

async def start_zmq_connection():
    global zmq_queue

    ctx = zmq.asyncio.Context()
    socket = ctx.socket(zmq.REQ)

    while True:
        try:
            socket.connect(f"tcp://{config['zmq']['ip_server']}:{config['zmq']['port']}")
            break
        except zmq.ZMQError as e:
            logging.error(f"ZMQError while connecting to server: {e}")
            await asyncio.sleep(1)

    zmq_queue = asyncio.Queue()

    async def zmq_worker():
        while True:
            message = await zmq_queue.get()
            while True:  # Loop until message is successfully sent and reply received.
                try:
                    await socket.send_string(message)
                    reply = await socket.recv_string()
                    break  # If sending and receiving were successful, exit the loop.
                except zmq.ZMQError as e:
                    logging.error(f"ZMQError while sending/receiving message: {e}")
                    logging.error("Trying to reconnect...")
                    while True:
                        try:
                            socket.close()  # Close the old socket
                            socket = ctx.socket(zmq.REQ)  # Create a new socket
                            socket.connect(f"tcp://{config['zmq']['ip_server']}:{config['zmq']['port']}")  # Try to reconnect
                            break  # If connection was successful, exit the loop
                        except zmq.ZMQError as e:
                            logging.error(f"ZMQError while connecting to server: {e}")
                            await asyncio.sleep(1)  # If not successful, wait and try again

            zmq_queue.task_done()
            zmq_queue.put_nowait(reply)

    asyncio.create_task(zmq_worker())

#  async def start_zmq_connection():
#      global zmq_queue
#
#      ctx = zmq.asyncio.Context()
#      socket = ctx.socket(zmq.REQ)
#
#      while True:
#          try:
#              socket.connect(f"tcp://{config['zmq']['ip_server']}:{config['zmq']['port']}")
#              break
#          except zmq.ZMQError as e:
#              logging.error(f"ZMQError while connecting to server: {e}")
#              await asyncio.sleep(1)
#
#      zmq_queue = asyncio.Queue()
#
#      async def zmq_worker():
#          while True:
#              message = await zmq_queue.get()
#              try:
#                  await socket.send_string(message)
#                  reply = await socket.recv_string()
#                  zmq_queue.task_done()
#                  zmq_queue.put_nowait(reply)
#              except zmq.ZMQError as e:
#                  logging.error(f"ZMQError while sending/receiving message: {e}")
#                  zmq_queue.task_done()
#                  zmq_queue.put_nowait(None)
#
#      asyncio.create_task(zmq_worker())

@app.before_serving
async def init():
    await start_zmq_connection()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

async def generate_thumbnail_path(video_filename):
    video_filename = os.path.basename(video_filename)
    thumbnail_filename = f"{os.path.splitext(video_filename)[0]}_thumbnail.jpg"
    thumbnail_path = os.path.join(video_dir, thumbnail_filename)

    if not os.path.exists(thumbnail_path):
        video_path = os.path.join(video_dir, video_filename)
        if not os.path.exists(video_path):
            return None

        clip = VideoFileClip(video_path)
        midpoint = clip.duration / 2

        try:
            clip.save_frame(thumbnail_path, t=midpoint)
        except Exception as e:
            logging.error(f"Error generating thumbnail: {e}")
            return None

    return thumbnail_path

def validate_input(schema):
    v = Validator(schema)
    def decorator(func):
        async def wrapper(*args, **kwargs):
            payload = await request.get_json(force=True)
            if v.validate(payload):
                return await func(*args, **kwargs, payload=v.document)
            return jsonify({"error": v.errors}), 400
        return wrapper
    return decorator

video_upload_schema = {
    "file": {"type": "string", "required": True}
}

playlist_update_schema = {
    "playlist": {
        "type": "list", "schema": {
            "type": "dict", "schema": {
                "name": {"type": "string", "required": True},
                "filepath": {"type": "string", "required": True},
                "thumbnail": {"type": "string", "required": True}
            }
        }, "required": True
    },
    "mode": {"type": "string", "required": True}
}

@app.route("/api/state", methods=["GET", "POST"])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def set_state():
    if request.method == "POST":
        return await set_state_post()

    if request.method == "GET":
        return await set_state_get()

    return jsonify({"error": "Invalid request method"}), 405

@app.route("/api/mode", methods=["GET", "POST"])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
async def set_mode():
    if request.method == "POST":
        return await set_mode_post()

    if request.method == "GET":
        return await set_mode_get()

    return jsonify({"error": "Invalid request method"}), 405

@validate_input({"state": {"type": "string", "required": True}})
async def set_state_post(payload=None):
    state = payload.get("state")
    zmq_queue.put_nowait(state)
    reply = await zmq_queue.get()
    zmq_queue.task_done()
    zmq_queue.put_nowait(reply)
    return jsonify({"success": True, "reply": reply})

async def set_state_get():
    state = await zmq_queue.get()
    zmq_queue.task_done()
    zmq_queue.put_nowait(state)
    return jsonify({"success": True, "state": state})

@validate_input({"mode": {"type": "string", "required": True}})
async def set_mode_post(payload=None):
    mode = payload.get("mode")
    zmq_queue.put_nowait(mode.upper())
    reply = await zmq_queue.get()
    zmq_queue.task_done()
    zmq_queue.put_nowait(reply)
    return jsonify({"success": True, "reply": reply})

async def set_mode_get():
    mode = await zmq_queue.get()
    zmq_queue.task_done()
    zmq_queue.put_nowait(mode)
    return jsonify({"success": True, "mode": mode})

#  @app.route("/api/state", methods=["GET", "POST"])
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  @validate_input({"state": {"type": "string", "required": True}})
#  async def set_state(payload=None):
#      if request.method == "POST":
#          state = payload.get("state")
#          zmq_queue.put_nowait(state)
#          reply = await zmq_queue.get()
#          zmq_queue.task_done()
#          zmq_queue.put_nowait(reply)
#          return jsonify({"success": True, "reply": reply})
#
#      if request.method == "GET":
#          state = await zmq_queue.get()
#          zmq_queue.task_done()
#          zmq_queue.put_nowait(state)
#          return jsonify({"success": True, "state": state})
#
#      return jsonify({"error": "Invalid request method"}), 405
#
#  @app.route("/api/mode", methods=["GET", "POST"])
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  @validate_input({"mode": {"type": "string", "required": True}})
#  async def set_mode(payload=None):
#      if request.method == "POST":
#          mode = payload.get("mode")
#          zmq_queue.put_nowait(mode.upper())
#          reply = await zmq_queue.get()
#          zmq_queue.task_done()
#          zmq_queue.put_nowait(reply)
#          return jsonify({"success": True, "reply": reply})
#
#      if request.method == "GET":
#          mode = await zmq_queue.get()
#          zmq_queue.task_done()
#          zmq_queue.put_nowait(mode)
#          return jsonify({"success": True, "mode": mode})
#
#      return jsonify({"error": "Invalid request method"}), 405

@app.route("/api/playlist", methods=["GET", "POST"])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
@validate_input(playlist_update_schema)
async def handle_playlist(payload=None):
    playlist_path = os.path.join(video_dir, "playlist.json")

    if request.method == "GET":
        async with aiofiles.open(playlist_path, "r") as f:
            playlist = json.loads(await f.read())
        return jsonify(playlist)

    if request.method == "POST":
        async with aiofiles.open(playlist_path, "w") as f:
            await f.write(json.dumps(payload))
        zmq_queue.put_nowait("set_playlist")
        return jsonify({"success": True})

    return jsonify({"error": "Invalid request method"}), 405

@app.route("/api/videos", methods=['GET', 'POST', 'DELETE'])
@route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
@validate_input(video_upload_schema)
async def handle_videos(payload=None):
    if request.method == "GET":
        videos = []
        for filename in os.listdir(video_dir):
            if os.path.isfile(os.path.join(video_dir, filename)) and allowed_file(filename):
                filepath = os.path.join(video_dir, filename)
                thumbnail = await generate_thumbnail_path(filepath)
                if thumbnail:
                    video = {
                        "name": filename,
                        "filepath": filepath,
                        "thumbnail": thumbnail
                    }
                    videos.append(video)
        return jsonify({"mediaFiles": videos})

    if request.method == "POST":
        file = payload.get("file")

        if file is None:
            return jsonify({"error": "No file part"}), 400

        if not allowed_file(file.filename):
            return jsonify({"error": "Invalid file extension"}), 400

        filename = secure_filename(file.filename)
        filepath = os.path.join(video_dir, filename)

        file.save(filepath)
        thumbnail = await generate_thumbnail_path(filepath)

        if not thumbnail:
            return jsonify({"error": "Thumbnail generation failed"}), 500

        video = {
            "name": filename,
            "filepath": filepath,
            "thumbnail": thumbnail
        }

        return jsonify({"success": True, "video": video})

    if request.method == "DELETE":
        filename = payload.get("filename")
        filepath = os.path.join(video_dir, filename)
        os.remove(filepath)
        return jsonify({"success": True})

    return jsonify({"error": "Invalid request method"}), 405

if __name__ == "__main__":
    app.run(host=config['app']['ip'], port=config['app']['port'])


#  import os
#  import json
#  import zmq
#  import zmq.asyncio
#  from queue import Queue
#  from quart import Quart, websocket, send_file, request, jsonify
#  from quart_cors import cors, route_cors
#
#  #thumbnail
#  from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
#  from moviepy.editor import VideoFileClip
#  import hashlib
#
#
#  from werkzeug.utils import secure_filename
#
#  import aiofiles
#  import asyncio
#
#  import logging
#
#  app = Quart(__name__)
#  #  app = cors(app, allow_origin="*")
#  app = cors(app, allow_origin="*", allow_headers="*", allow_methods="*")
#
#  @app.before_serving
#  async def startup():
#      global zmq_lock
#      zmq_lock = asyncio.Lock()
#
#  def load_config(config_file):
#      with open(config_file, 'r') as f:
#          config = json.load(f)
#      return config
#
#  def get_log_level( level):
#      levels = {
#          'DEBUG': logging.DEBUG,
#          'INFO': logging.INFO,
#          'WARNING': logging.WARNING,
#          'ERROR': logging.ERROR,
#          'CRITICAL': logging.CRITICAL
#      }
#      return levels.get(level.upper(), logging.INFO)
#
#  config = load_config('config/config.json')
#
#  video_dir = config['video_dir']
#  logging.basicConfig(level=get_log_level(config['debug']['log_level']))
#
#  ALLOWED_EXTENSIONS = config['video_ext']
#
#  ctx = zmq.asyncio.Context()
#  socket = ctx.socket(zmq.REQ)
#
#  socket.connect(f"tcp://{config['zmq']['ip_server']}:{config['zmq']['port']}")  # Connect to the player app
#  #  socket.connect("tcp://player:5555")
#
#  def allowed_file(filename):
#      return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
#
#
#  async def send_message_to_player(message):
#      global zmq_lock
#      async with zmq_lock:
#          try:
#              logging.debug(f"Sending message: {message}")
#              await socket.send_string(message)
#              reply = await socket.recv_string()
#              return reply
#          except zmq.ZMQError as e:
#              logging.error(f"ZMQError while sending/receiving message: {e}")
#              return -1
#  #  def send_message_to_player(message):
#  #      try:
#  #          logging.info(f"Sending message: {message}")
#  #          socket.send_string(message)
#  #          reply = socket.recv_string()
#  #          return reply
#  #      except zmq.ZMQError as e:
#  #          logging.error(f"Encountered ZMQError while sending/receiving message: {e}")
#  #          return None
#
#
#  def generate_thumbnail_path(video_filename):
#      video_filename = os.path.basename(video_filename)
#      thumbnail_filename = f"{os.path.splitext(video_filename)[0]}_thumbnail.jpg"
#      thumbnail_path = os.path.join(video_dir, thumbnail_filename)
#
#      video_path = os.path.join(video_dir, video_filename)
#      if not os.path.exists(thumbnail_path):
#          if "ww" in ALLOWED_EXTENSIONS:
#              return "no thumbnails for ww"
#          # Load video file
#          clip = VideoFileClip(video_path)
#
#          # Find the middle frame of the video
#          midpoint = clip.duration / 2
#
#          # Save the thumbnail image
#          clip.save_frame(thumbnail_path, t=midpoint)
#
#      return thumbnail_path
#
#
#  @app.route("/api/state", methods=["GET", "POST"])
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  async def set_state():
#      if request.method == "POST":
#          logging.debug('Received a POST STATE request')
#          form_data = await request.form
#          state = form_data.get("state")
#
#          if state is None:
#              logging.error("Error: state post request is None")
#              return jsonify({"error": "State is None"}), 400
#
#          reply = await send_message_to_player(state)
#          #  if reply is None:
#          #      app.logger.error(f"Error POST STATE response: {reply}")
#          #      return jsonify({"error": "An error occurred while communicating with the player"}), 500
#
#          app.logger.debug(f" POST STATE response: {reply}")
#          return jsonify({"success": True, "reply": reply})
#
#      if request.method == "GET":
#          logging.debug('Received a GET STATE request')
#          #  state = "playing"
#          state = await send_message_to_player("get_state")
#          #  #  if state is None:
#          #  #      app.logger.error(f"Error POST state response is NONE")
#          #  #      return jsonify({"error": "An error occurred while communicating with the player"}), 500
#          app.logger.debug(f" GET STATE from player response: {state}")
#          return jsonify({"success": True, "state": state})
#
#      logging.error("STATE Error: Invalid request method")
#      return jsonify({"error": "Invalid request method"}), 405
#
#  @app.route("/api/mode", methods=["GET", "POST"])
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  async def set_mode():
#      if request.method == "POST":
#          logging.debug('Received a POST request')
#          form_data = await request.form
#          mode = form_data.get("mode")
#
#          if mode is None:
#              logging.error("Error: mode is None")
#              return jsonify({"error": "mode is None"}), 400
#
#          reply = await send_message_to_player(mode.upper())
#          logging.debug(f"GET MODE Reply from player: {reply}")
#          return jsonify({"success": True, "reply": reply})
#
#      if request.method == "GET":
#          #  logging.debug('Received a GET request')
#          mode = await send_message_to_player("get_mode")
#          if mode is None:
#              app.logger.error(f"Error POST state response is NONE")
#              return jsonify({"error": "An error occurred while communicating with the player"}), 500
#          app.logger.debug(f" GET state response: {mode}")
#          return jsonify({"success": True, "mode": mode, "reply": mode})
#
#      logging.error("MODE Error: Invalid request method")
#      return jsonify({"error": "Invalid request method"}), 405
#
#  @app.route("/api/playlist", methods=["GET", "POST"])
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  async def handle_playlist():
#      playlist_path = os.path.join(video_dir, "playlist.json")
#      if request.method == "GET":
#      #  # Replace this part with static response
#      #      playlist = {
#      #          "playlist": [
#      #              {"name": "Sample Video 1", "filepath": "/path/to/sample1.mp4", "thumbnail": "/path/to/sample1_thumbnail.jpg"},
#      #              {"name": "Sample Video 2", "filepath": "/path/to/sample2.mp4", "thumbnail": "/path/to/sample2_thumbnail.jpg"},
#      #          ],
#      #          "mode": "repeat"
#      #      }
#          async with aiofiles.open(playlist_path, "r") as f:
#              playlist = json.loads(await f.read())
#          logging.debug(f"GET PLAYLIST response: {playlist}")
#          return jsonify(playlist)
#      elif request.method == "POST":
#          playlist = await request.get_json()
#          async with aiofiles.open(playlist_path, "w") as f:
#              await f.write(json.dumps(playlist))
#          await send_message_to_player("set_playlist")
#          return jsonify({"success": True})
#
#      logging.error("PLAYLIST Error: Invalid request method")
#      return jsonify({"error": "Invalid request method"}), 405
#
#
#  @app.route("/api/brightness", methods=["GET", "POST"])
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  async def handle_brightness():
#      if request.method == "POST":
#          #  logging.debug('Received a POST BRIGHTNESS request')
#          form_data = await request.form
#          brightness = float(form_data.get("brightness"))
#          brightness = int(brightness/100.0 * 255.0)
#          reply = await send_message_to_player(f"set_brightness {brightness}")
#          #  logging.debug(f"Brightness from player: {reply}")
#          return jsonify({"success": True, "reply": reply})
#
#      if request.method == "GET":
#          #  logging.debug('Received a GET BRIGHTNESS request')
#          brightness = await send_message_to_player("get_brightness")
#          brightness = float(brightness) / 255.0
#          #  app.logger.debug(f" GET Brightness response: {brightness}")
#          return jsonify({"success": True, "brightness": brightness})
#
#      logging.error("BRIGHTNESS Error: Invalid request method")
#      return jsonify({"error": "Invalid request method"}), 405
#
#  @app.route('/api/fps', methods=['GET', 'POST'])
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  async def set_fps():
#      if request.method == "GET":
#          #  logging.debug('Received a GET FPS request')
#          fps = await send_message_to_player("get_fps")
#          #  logging.debug(f"FPS from player: {fps}")
#          return jsonify({"success": True, "fps": float(fps)})
#
#      if request.method == "POST":
#          form_data = await request.form
#          fps = int(float(form_data.get("fps")))
#          reply = await send_message_to_player(f"set_fps {fps}")
#          return jsonify({"success": True, "reply": reply})
#
#      logging.error("FPS Error: Invalid request method")
#      return jsonify({"error": "Invalid request method"}), 405
#
#  @app.route("/api/videos", methods=['GET', 'POST', 'DELETE'])
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  async def handle_videos():
#      if request.method == "GET":
#          videos = [
#              {
#                  "name": f,
#                  "filepath": os.path.join(video_dir, f),
#                  "thumbnail": generate_thumbnail_path(f)  # Assuming you have a function to generate thumbnail paths
#              }
#              for f in os.listdir(video_dir)
#              if os.path.isfile(os.path.join(video_dir, f)) and allowed_file(f)
#          ]
#          return jsonify({"mediaFiles": videos})
#
#      if request.method == "POST":
#          file = (await request.files).get("file")
#          if not file or file.filename == "":
#              return jsonify({"error": "No file selected or file name is empty"}), 400
#          if allowed_file(file.filename):
#              filename = secure_filename(file.filename)
#              await file.save(os.path.join(video_dir, filename))
#              #  generate_thumbnail_path(filename)
#              logging.debug(f"File {filename} saved")
#              return jsonify({"success": True})
#          return jsonify({"error": "Unsupported file type"}), 400
#
#      if request.method == "DELETE":
#          filename = (await request.form).get("filename")
#          if not filename:
#              return jsonify({"error": "Filename is missing"}), 400
#          file_path = os.path.join(video_dir, filename)
#          if os.path.exists(file_path):
#              os.remove(file_path)
#              return jsonify({"success": True})
#          return jsonify({"error": "File not found"}), 404
#
#      return jsonify({"error": "Unsupported method"}), 405
#
#
#  @app.route("/api/currentMedia", methods=["GET"])
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  async def get_current_media():
#
#      current_media = await send_message_to_player("get_current_media")
#      if current_media is None:
#          app.logger.error("Error getting current media response")
#          return jsonify({"error": "An error occurred while communicating with the player"}), 500
#
#      elif type(current_media) == str:
#          video_file = {
#              "name": current_media,
#              "filepath": os.path.join(video_dir, current_media),
#              "thumbnail": generate_thumbnail_path(current_media)
#          }
#          logging.debug(f"GET CURRENT MEDIA response: {video_file}")
#          return jsonify(video_file)
#
#  @app.route("/thumbnails/<filename>")
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  async def serve_thumbnail(filename):
#      logging.debug(f"Thumbnail request for {filename}")
#      thumbnail_path = generate_thumbnail_path(filename)
#      logging.debug(f"Thumbnail path: {thumbnail_path}")
#      return await send_file(thumbnail_path)
#
#  @app.websocket('/stream')
#  @route_cors(allow_origin="*", allow_headers="*", allow_methods="*")
#  async def stream():
#      async with aiofiles.open('content/box_test.mov', mode='rb') as f:
#          while True:
#              data = await f.read(1024)
#              if not data:
#                  break
#              await websocket.send(data)
#              await asyncio.sleep(0.1)
#
#  if __name__ == '__main__':
#      app.run(host = f"{config['rest_api']['ip']}", port = int(config['rest_api']['port']))
#

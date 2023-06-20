import time
import zmq
import zmq.asyncio
import asyncio
import logging

LAST_MSG_TIME = time.time()


async def socket_connect_backoff(sub_socket, ip_connect, port):
    sub_socket.connect(f"tcp://{ip_connect}:{port}")
    sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")
    #  delay = 1.0
    #  max_delay = 30.0
    #  while True:
    #      try:
    #          logging.info(f"Connecting to {ip_connect}:{port}")
    #          sub_socket.connect(f"tcp://{ip_connect}:{port}")
    #          sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")
    #          break
    #      except zmq.ZMQError:
    #          logging.error("Unable to establish connection, retrying in {} seconds...".format(delay))
    #          await asyncio.sleep(delay)
    #          delay = min(delay * 2, max_delay)
    #


async def listen_to_messages(sub_socket, process_message):
    logging.info(f"Started listening to messages {sub_socket}")
    logging.debug(f"socket port: {sub_socket.getsockopt(zmq.LAST_ENDPOINT)}")
    while True:
        try:
            logging.debug("Waiting for message")
            message = await sub_socket.recv_string()
            logging.debug("Received message: " + message)
            await process_message(message)
        except Exception as e:
            logging.error("Error processing message: "+ str(e))

        await asyncio.sleep(0.01)


async def reset_socket(sub_socket, config):
    logging.debug("Resetting socket")
    # close the current socket
    sub_socket.close()
    # create a new socket
    new_sock = zmq.asyncio.Context().socket(zmq.SUB)
    logging.debug(f"Subscribing to tcp://{config['zmq']['ip_connect']}:{config['zmq']['port_player_pub']}")
    # connect the new socket
    try:
        new_sock.connect(f"tcp://{config['zmq']['ip_connect']}:{config['zmq']['port_player_pub']}")
        new_sock.setsockopt_string(zmq.SUBSCRIBE, "")
    except zmq.ZMQError as zmq_error:
        logging.error(f"Subscribing to tcp://{config['zmq']['ip_connect']}:{config['zmq']['port_player_pub']}")
        logging.error(f"ZMQ Error occurred during socket reset: {str(zmq_error)}")
    
    return new_sock

async def monitor_socket(sub_socket, config):
    #monitor sub_socket and if it's been too long since LAST_MSG_TIME, reset the socket
    global LAST_MSG_TIME
    LAST_MSG_TIME = time.time()

    logging.debug("Monitoring socket")
    while True:

        logging.debug(f"Time since last message: {time.time() - LAST_MSG_TIME}")
        if time.time() - LAST_MSG_TIME > 10:
            logging.debug("Resetting socket")
            fut = asyncio.ensure_future(sub_socket.recv())
            try:
                resp = await asyncio.wait_for(fut, timeout=0.5)  # Close the previous socket only after a short time-out
                LAST_MSG_TIME = time.time()
                logging.debug("New message received, not resetting the socket!")
            except asyncio.TimeoutError:
                sub_socket = await reset_socket(sub_socket, config)
                LAST_MSG_TIME = time.time()

        await asyncio.sleep(1)



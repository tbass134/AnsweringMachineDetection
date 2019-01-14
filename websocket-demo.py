#!/usr/bin/env python

from __future__ import absolute_import, print_function

import io
import logging
import os
import sys
import time
from logging import debug, info
import uuid
import cgi
import nexmo

import requests
import tornado.ioloop
import tornado.websocket
import tornado.httpserver
import tornado.template
import tornado.web
import webrtcvad
from tornado.web import url
import json

from base64 import b64decode

#Only used for record function
import datetime
import wave

import numpy as np
from scipy.io import wavfile
import librosa
import pickle

from dotenv import load_dotenv
load_dotenv()

os.environ['KMP_DUPLICATE_LIB_OK']='True'

logging.captureWarnings(True)


# Constants:
MS_PER_FRAME = 15  # Duration of a frame in ms
HOSTNAME =  os.getenv("HOSTNAME")#Change to the hostname of your server
NEXMO_NUMBER = os.getenv("NEXMO_NUMBER")
NEXMO_APP_ID = os.getenv("NEXMO_APP_ID")
CONF_NAME = os.getenv("CONF_NAME")

# Global variables
conns = {}
conversation_ids = set()
beep_captured = False
loaded_model = pickle.load(open("models/rf-mfccs_40-10s-2.pkl", "rb"))
print(loaded_model)
client = nexmo.Client(application_id=NEXMO_APP_ID, private_key=NEXMO_APP_ID+".key")

class BufferedPipe(object):
    def __init__(self, max_frames, sink):
        """
        Create a buffer which will call the provided `sink` when full.

        It will call `sink` with the number of frames and the accumulated bytes when it reaches
        `max_buffer_size` frames.
        """
        self.sink = sink
        self.max_frames = max_frames

        self.count = 0
        self.payload = b''

    def append(self, data, id):
        """ Add another data to the buffer. `data` should be a `bytes` object. """

        self.count += 1
        self.payload += data

        if self.count == self.max_frames:
            self.process(id)

    def process(self, id):
        """ Process and clear the buffer. """
        self.sink(self.count, self.payload, id)
        self.count = 0
        self.payload = b''


class LexProcessor(object):
    def __init__(self, path, rate, clip_min, aws_region, aws_id, aws_secret):
        self._aws_region = aws_region
        self._aws_id = aws_id
        self._aws_secret = aws_secret
        self.rate = rate
        self.bytes_per_frame = rate/25
        self._path = path
        self.clip_min_frames = clip_min // MS_PER_FRAME
    def process(self, count, payload, id):
        if beep_captured == False and count > self.clip_min_frames:  # If the buffer is less than CLIP_MIN_MS, ignore it
            fn = "{}rec-{}-{}.wav".format('', id, datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
            output = wave.open(fn, 'wb')
            output.setparams((1, 2, self.rate, 0, 'NONE', 'not compressed'))
            output.writeframes(payload)
            output.close()
            debug('File written {}'.format(fn))
            self.process_file(fn)
            info('Processing {} frames for {}'.format(str(count), id))
        else:
            info('Discarding {} frames'.format(str(count)))
    def playback(self, response, id):
        if self.rate == 8000:
            content, _ignore = audioop.ratecv(response, 2, 1, 16000, 8000, None) # Downsample 16Khz to 8Khz
        else:
            content = response
        frames = len(content) // self.bytes_per_frame
        info("Playing {} frames to {}".format(frames, id))
        conn = conns[id]
        pos = 0
        for x in range(0, frames + 1):
            newpos = pos + self.bytes_per_frame
            #debug("writing bytes {} to {} to socket for {}".format(pos, newpos, id))
            data = content[pos:newpos]
            conn.write_message(data, binary=True)
            pos = newpos
    def process_file(self, wav_file):
        if loaded_model != None:
            print("load file {}".format(wav_file))
            X, sample_rate = librosa.load(wav_file, res_type='kaiser_fast')
            mfccs = np.mean(librosa.feature.mfcc(y=X, sr=sample_rate, n_mfcc=40).T,axis=0)
            X = [mfccs]
            prediction = loaded_model.predict(X)
            print("prediction",prediction)
            if prediction[0] == 0:
                global beep_captured
                beep_captured = True
                print("beep")
                # for id in conversation_ids:
                #     print("sending speech to ", id)
                #     response = client.send_speech(id, text='We have detected your answering machine. Thats ok, we\'ll call you back later')
                # time.sleep(10)
                # for id in conversation_ids:
                #     print("sending speech to ", id)
                #     client.update_call(id, action='hangup')

        else:
            print("model not loaded")
    def removeFile(self, wav_file):
         os.remove(wav_file)
    def playAudio(self, uuid):
        response = client.send_speech(uuid, text='Hello')


class WSHandler(tornado.websocket.WebSocketHandler):
    def initialize(self):
        # Create a buffer which will call `process` when it is full:
        self.frame_buffer = None
        # Setup the Voice Activity Detector
        self.tick = None
        self.id = uuid.uuid4().hex
        self.vad = webrtcvad.Vad()
          # Level of sensitivity
        self.processor = None
        self.path = None
        self.rate = None #default to None
        self.silence = 20 #default of 20 frames (400ms)
        conns[self.id] = self
    def open(self, path):
        info("client connected")
        debug(self.request.uri)
        self.path = self.request.uri
        self.tick = 0
    def on_message(self, message):
        # Check if message is Binary or Text
        if type(message) != str:
            # print("message",message)
            # print(self.rate)
            if self.vad.is_speech(message, self.rate):
                debug ("SPEECH from {}".format(self.id))
                self.tick = self.silence
                self.frame_buffer.append(message, self.id)
            else:
                debug("Silence from {} TICK: {}".format(self.id, self.tick))
                self.tick -= 1
                if self.tick == 0:
                    self.frame_buffer.process(self.id)  # Force processing and clearing of the buffer
        else:
            # Here we should be extracting the meta data that was sent and attaching it to the connection object
            data = json.loads(message)
            print(data)
            if data.get('content-type'):
                m_type, m_options = cgi.parse_header(data['content-type'])
                self.rate = 16000
                # region = data.get('aws_region', 'us-east-1')
                clip_min = int(data.get('clip_min', 200))
                clip_max = int(data.get('clip_max', 10000))
                silence_time = int(data.get('silence_time', 300))
                sensitivity = int(data.get('sensitivity', 3))
                self.vad.set_mode(sensitivity)
                self.silence = silence_time // MS_PER_FRAME
                self.processor = LexProcessor(self.path, self.rate, clip_min, None, None, None).process
                self.frame_buffer = BufferedPipe(clip_max // MS_PER_FRAME, self.processor)
                self.write_message('ok')
    def on_close(self):
        # Remove the connection from the list of connections
        del conns[self.id]
        info("client disconnected")


class PingHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self):
        self.write('ok')
        self.set_header("Content-Type", 'text/plain')
        self.finish()

class EventHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def post(self):
        # print(self.request.body)
        data = json.loads(self.request.body)

        if data["status"] == "answered":
            print(data)
            global conversation_ids
            conversation_ids.add(data["uuid"])
            # print("conversation_ids",conversation_ids)
        self.content_type = 'text/plain'
        self.write('ok')
        self.finish()

class EnterPhoneNumberHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self):
        ncco = [
              {
                "action": "talk",
                "text": "Please enter a phone number to dial"
              },
              {
                "action": "input",
                "eventUrl": ["https://"+HOSTNAME+"/ivr"],
                "timeOut":10,
                "maxDigits":12,
                "submitOnHash":True
              },
              {
              "action": "conversation",
              "name": CONF_NAME
              }
            ]
        self.write(json.dumps(ncco))
        self.set_header("Content-Type", 'application/json; charset="utf-8"')
        self.finish()


class AcceptNumberHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def post(self):
        data = json.loads(self.request.body)
        print(data["dtmf"])
        response = client.create_call({
          'to': [{'type': 'phone', 'number': data["dtmf"]}],
          'from': {'type': 'phone', 'number': NEXMO_NUMBER},
          'answer_url': ["https://"+HOSTNAME+"/ncco-connect"]
        })
        # conversation_uuid = response["uuid"]
        # print(conversation_uuid)
        # response = client.send_speech(conversation_uuid, text='Hello')

        self.content_type = 'text/plain'
        self.write('ok')
        self.finish()

class CallHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self):
        ncco = [

            {
               "action": "connect",
               "from": NEXMO_NUMBER,
               "endpoint": [
                   {
                      "type": "websocket",
                      "uri" : "ws://"+HOSTNAME+"/socket",
                      "content-type": "audio/l16;rate=16000",
                      "headers": {
                      }
                   }
               ]
             },
             {
             "action": "conversation",
             "name": CONF_NAME
             }
        ]
        self.write(json.dumps(ncco))
        self.set_header("Content-Type", 'application/json; charset="utf-8"')
        self.finish()


def main():
    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)7s %(message)s",
        )
        application = tornado.web.Application([
			url(r"/ping", PingHandler),
            (r"/event", EventHandler),
            (r"/ncco", CallHandler),
            (r"/ncco-connect", CallHandler),
            (r"/ivr", AcceptNumberHandler),
            url(r"/(.*)", WSHandler),
        ])
        http_server = tornado.httpserver.HTTPServer(application)
        http_server.listen(8000)
        tornado.ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        pass  # Suppress the stack-trace on quit


if __name__ == "__main__":
    main()
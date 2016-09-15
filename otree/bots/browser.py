#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import

import json
import threading
import logging
from collections import OrderedDict

from django.test import SimpleTestCase

import channels
import traceback

import otree.common_internal
from otree.models.participant import Participant
from otree.common_internal import get_redis_conn, SESSION_CODE_CHARSET

from .bot import ParticipantBot
import time

REDIS_KEY_PREFIX = 'otree-bots'

SESSIONS_PRUNE_LIMIT = 50

# global variable that holds the browser bot worker instance in memory
browser_bot_worker = None  # type: Worker

prepare_submit_lock = threading.Lock()

logger = logging.getLogger('otree.test.browser_bots')

def make_redis_key(participant_code):
    return '{}-{}'.format(REDIS_KEY_PREFIX, participant_code[0])


class Worker(object):
    def __init__(self, redis_conn=None, char_range=None):
        self.redis_conn = redis_conn
        self.browser_bots = {}
        self.prepared_submits = {}
        char_range = char_range or SESSION_CODE_CHARSET
        self.redis_listen_keys = ['{}-{}'.format(REDIS_KEY_PREFIX, char)
                                  for char in char_range]


    def initialize_participant(self, participant_code):
        participant = Participant.objects.get(code=participant_code)
        self.prune()

        # in order to do .assertEqual etc, need to pass a reference to a
        # SimpleTestCase down to the Player bot
        test_case = SimpleTestCase()

        self.browser_bots[participant.code] = ParticipantBot(
            participant, unittest_case=test_case)
        return {'ok': True}

    def get_method(self, command_name):
        commands = {
            'prepare_next_submit': self.prepare_next_submit,
            'consume_next_submit': self.consume_next_submit,
            'initialize_participant': self.initialize_participant,
            'clear_all': self.clear_all,
            'ping': self.ping,
        }

        return commands[command_name]

    def prune(self):
        '''to avoid memory leaks'''
        # FIXME
        pass

    def clear_all(self):
        self.browser_bots.clear()

    def consume_next_submit(self, participant_code):
        submission = self.prepared_submits.pop(participant_code)
        # maybe was popped in prepare_next_submit
        submission.pop('page_class', None)
        return submission

    def prepare_next_submit(self, participant_code, path, html):

        try:
            bot = self.browser_bots[participant_code]
        except KeyError:
            return {
                'request_error': (
                    "Participant {} not loaded in botworker. "
                    "The botworker only stores the most recent {} sessions, "
                    "and discards older sessions. Or, maybe the botworker "
                    "was restarted after the session was created.".format(
                        participant_code, SESSIONS_PRUNE_LIMIT)
                )
            }

        # so that any asserts in the PlayerBot work.
        bot.path = path
        bot.html = html

        with prepare_submit_lock:
            if participant_code in self.prepared_submits:
                return {}

            try:
                submission = next(bot.submits_generator)
            except StopIteration:
                # don't prune it because can cause flakiness if
                # there are other GET requests coming in. it will be pruned
                # when new sessions are created anyway.

                # need to return something, to distinguish from Redis timeout
                submission = {}
            else:
                # because we are returning it through Redis, need to pop it
                # here
                submission.pop('page_class')

            self.prepared_submits[participant_code] = submission

        return submission

    def ping(self, *args, **kwargs):
        return {'ok': True}

    def redis_listen(self):
        print('botworker is listening for messages through Redis')
        while True:
            retval = None

            # blpop returns a tuple
            result = None

            # put it in a loop so that we can still receive KeyboardInterrupts
            # otherwise it will block
            idle_start = time.time()
            while result is None:
                result = self.redis_conn.blpop(
                    self.redis_listen_keys, timeout=3)
            logger.info('idle for {}'.format(round(time.time() - idle_start, 3)))
            busy_start = time.time()

            key, message_bytes = result
            message = json.loads(message_bytes.decode('utf-8'))
            response_key = message['response_key']

            try:
                cmd = message['command']
                args = message.get('args', [])
                kwargs = message.get('kwargs', {})
                method = self.get_method(cmd)
                retval = method(*args, **kwargs)
            except Exception as exc:
                # request_error means the request received through Redis
                # was invalid.
                # response_error means the botworker raised while processing
                # the request.
                retval = {
                    'response_error': repr(exc),
                    'traceback': traceback.format_exc()
                }
                # don't raise, because then this would crash.
                # logger.exception() will record the full traceback
                logger.exception(repr(exc))
            finally:
                retval_json = json.dumps(retval or {})
                self.redis_conn.rpush(response_key, retval_json)
                logger.info('busy for {}'.format(round(time.time() - busy_start, 3)))


def ping(redis_conn, participant_code):
    response_key = '{}-ping-{}'.format(REDIS_KEY_PREFIX, participant_code)
    msg = {
        'command': 'ping',
        'response_key': response_key,
    }
    redis_conn.rpush(make_redis_key(participant_code), json.dumps(msg))
    result = redis_conn.blpop(response_key, timeout=1)

    if result is None:
        raise Exception(
            'Ping to botworker failed. '
            'If you want to use browser bots, '
            'you need to be running the botworker.'
            'Otherwise, set ("use_browser_bots": False) in the session config '
            'in settings.py.'
        )


def initialize_bot_redis(redis_conn, participant_code):
    response_key = '{}-initialize-{}'.format(REDIS_KEY_PREFIX, participant_code)
    msg = {
        'command': 'initialize_participant',
        'kwargs': {'participant_code': participant_code},
        'response_key': response_key,
    }
    # ping will raise if it times out
    ping(redis_conn, participant_code)
    redis_conn.rpush(make_redis_key(participant_code), json.dumps(msg))

    timeout=1
    result = redis_conn.blpop(response_key, timeout=timeout)
    if result is None:
        raise Exception(
            'botworker is running but could not initialize the session. '
            'within {} seconds.'.format(timeout)
        )
    key, submit_bytes = result
    value = json.loads(submit_bytes.decode('utf-8'))
    if 'response_error' in value:
        raise Exception(
            'An error occurred. See the botworker output for the traceback.')
    return {'ok': True}


def initialize_bot_in_process(participant_code):
    browser_bot_worker.initialize_participant(participant_code)


def initialize_bot(participant_code):
    if otree.common_internal.USE_REDIS:
        initialize_bot_redis(
            redis_conn=get_redis_conn(),
            participant_code=participant_code,
        )
    else:
        initialize_bot_in_process(participant_code)


def redis_flush_bots(redis_conn, char_range=None):
    if not char_range:
        char_range = SESSION_CODE_CHARSET
    for key in redis_conn.scan_iter(match='{}[{}]*'.format(
            REDIS_KEY_PREFIX, char_range)):
        redis_conn.delete(key)


class EphemeralBrowserBot(object):

    def __init__(self, view, redis_conn=None):
        self.view = view
        self.participant = view.participant
        self.session = self.view.session
        self.redis_conn = redis_conn or get_redis_conn()
        self.path = self.view.request.path

    def prepare_next_submit_redis(self, html):
        participant_code = self.participant.code
        redis_conn = self.redis_conn
        response_key = '{}-prepare_next_submit-{}'.format(
            REDIS_KEY_PREFIX, participant_code)
        msg = {
            'command': 'prepare_next_submit',
            'kwargs': {
                'participant_code': participant_code,
                'path': self.path,
                'html': html,
            },
            'response_key': response_key,
        }
        redis_conn.rpush(make_redis_key(participant_code), json.dumps(msg))
        # in practice is very fast...around 1ms
        # however, if an exception occurs, could take quite long.
        result = redis_conn.blpop(response_key, timeout=3)
        if result is None:
            # ping will raise if it times out
            ping(redis_conn, participant_code)
            raise Exception(
                'botworker is running but did not return a submission.'
            )
        key, submit_bytes = result
        return json.loads(submit_bytes.decode('utf-8'))

    def prepare_next_submit_in_process(self, html):
        return browser_bot_worker.prepare_next_submit(
            self.participant.code, self.path, html)

    def prepare_next_submit(self, html):
        if otree.common_internal.USE_REDIS:
            result = self.prepare_next_submit_redis(html)
            # response_error only exists if using Redis.
            # if using runserver, there is no need for this because the
            # exception is raised in the same thread.
            if 'response_error' in result:
                # cram the other traceback in this traceback message.
                # note:
                raise Exception(result['traceback'])
        else:
            result = self.prepare_next_submit_in_process(html)
        if 'request_error' in result:
            raise AssertionError(result['request_error'])

    def get_next_post_data_redis(self):
        participant_code = self.participant.code
        redis_conn = self.redis_conn
        response_key = '{}-consume_next_submit-{}'.format(
            REDIS_KEY_PREFIX, participant_code)
        msg = {
            'command': 'consume_next_submit',
            'kwargs': {
                'participant_code': participant_code,
            },
            'response_key': response_key,
        }
        redis_conn.rpush(make_redis_key(participant_code), json.dumps(msg))
        # in practice is very fast...around 1ms
        result = redis_conn.blpop(response_key, timeout=1)
        if result is None:
            # ping will raise if it times out
            ping(redis_conn, participant_code)
            raise Exception(
                'botworker is running but did not return a submission.'
            )
        key, submit_bytes = result
        return json.loads(submit_bytes.decode('utf-8'))

    def get_next_post_data(self):
        if otree.common_internal.USE_REDIS:
            submission = self.get_next_post_data_redis()
        else:
            submission = browser_bot_worker.prepared_submits.pop(
                self.participant.code)
        if submission:
            return submission['post_data']
        else:
            raise StopIteration('No more submits')

    def send_completion_message(self):
        channels.Group(
            'browser-bots-client-{}'.format(self.session.code)
        ).send({'text': self.participant.code})

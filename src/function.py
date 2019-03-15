'''
Copyright 2017 New Relic, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

This Lambda function receives log entries from CloudWatch Logs or S3 buckets
and pushes them to New Relic Infrastructure - Cloud integrations.

It expects to be invoked based on CloudWatch Logs streams although it's
ready to read log files from S3 buckets too.

New Relic's license key must be encrypted using KMS following these
instructions:

1. After creating te Lambda based on the Blueprint, select it and open the Environment Variables
section.

2. Check that the "LICENSE_KEY" environment variable if properly filled-in.

2. If you changed anything, go to the start of the page and press "Save". Logs should start to be processed by the Lambda.
To check if everything is functioning properly you can check the Monitoring tab and CloudWatch Logs

If you are using New Relic's EU region, you will need to add this environment variable to your function:

- NR_REGION: EU

For more detailed documentation, check New Relic's documentation site:
https://docs.newrelic.com/
'''
import os
import gzip
import json
import time
import boto3

from urllib import request
from io import StringIO
from base64 import b64decode
from enum import Enum


class EntryType(Enum):
    VPC = "vpc"
    LAMBDA = "lambda"
    OTHER = "other"


# New Relic Infractructure's ingest service. Do not modify.
INGEST_SERVICE_VERSION = 'v1'
US_INGEST_SERVICE_HOST = 'https://cloud-collector.newrelic.com'
EU_INGEST_SERVICE_HOST = 'https://cloud-collector.eu.newrelic.com'
INGEST_SERVICE_PATHS = {
    EntryType.LAMBDA: "/aws/lambda",
    EntryType.VPC: "/aws/vpc",
    EntryType.OTHER: "/aws",
}

# Retrying configuration.
# Increasing these numbers will make the function longer in case of
# communication failures and that will increase the cost.
# Decreasing these number could increase the probility of data loss.

# Maximum number of retries
MAX_RETRIES = 3
# Initial backoff (in seconds) between retries
INITIAL_BACKOFF = 1
# Multiplier factor for the backoff between retries
BACKOFF_MULTIPLIER = 2
# Max length in bytes of the payload
MAX_PAYLOAD_SIZE = 1000 * 1024


class MaxRetriesException(Exception):
    pass


class BadRequestException(Exception):
    pass


class ThrottlingException(Exception):
    pass


def http_retryable(func):
    '''
    Decorator that retries HTTP calls.

    The decorated function should perform an HTTP request and return its
    response.

    That function will be called until it returns a 200 OK response or 
    MAX_RETRIES is reached. In that case a MaxRetriesException will be raised.

    If the function returns a 4XX Bad Request, it will raise a BadRequestException
    without any retry unless it returns a 429 Too many requests. In that case, it
    will raise a ThrottlingException.
    '''
    def _format_error(e, text):
        return '{}. {}'.format(e, text)

    def wrapper_func():
        backoff = INITIAL_BACKOFF
        retries = 0

        while retries < MAX_RETRIES:
            if retries > 0:
                print('Retrying in {} seconds'.format(backoff))
                time.sleep(backoff)
                backoff *= BACKOFF_MULTIPLIER

            retries += 1

            try:
                response = func()

            # This exception is raised when receiving a non-200 response
            except request.HTTPError as e:
                if e.getcode() == 400:
                    raise BadRequestException(
                        _format_error(e, 'Unexpected payload'))
                elif e.getcode() == 403:
                    raise BadRequestException(
                        _format_error(e, 'Review your license key'))
                elif e.getcode() == 404:
                    raise BadRequestException(_format_error(
                        e, 'Review the region endpoint'))
                elif e.getcode() == 429:
                    raise ThrottlingException(
                        _format_error(e, 'Too many requests'))
                elif 400 <= e.getcode() < 500:
                    raise BadRequestException(e)

            # This exception is raised when the service is not responding
            except request.URLError as e:
                print('There was an error. Reason: {}'.format(e.reason))
            else:
                return response

        raise MaxRetriesException()

    return wrapper_func


def _get_log_type(event):
    '''
    This function determines if the given event is triggered by 
    CloudWatch Logs or S3's ObjectCreated action.
    '''
    if 'awslogs' in event:
        return 'cw_logs'
    elif ('Records' in event
            and 's3' in event['Records'][0]
            and 'ObjectCreated' in event['Records'][0]['eventName']):

        return 's3'

    return 'unknown'


def _get_s3_data(bucket, key):
    '''
    This function gets a specific log file from the given S3 bucket and
    decompresses it.
    '''
    s3_client = boto3.client('s3')
    data = s3_client.get_object(Bucket=bucket, Key=key)['Body'].read()

    if key.split('.')[-1] == 'gz':
        data = gzip.GzipFile(fileobj=StringIO(data)).read()

    return data


def _send_log_entry(log_entry, context):
    '''
    This function sends the log entry to New Relic Infrastructure's ingest
    server. If it is necessary, entries will be split in different payloads
    Log entry is sent along with the Lambda function's execution context
    '''
    data = {
        'context': {
            'function_name': context.function_name,
            'invoked_function_arn': context.invoked_function_arn,
            'log_group_name': context.log_group_name,
            'log_stream_name': context.log_stream_name
        },
        'entry': log_entry
    }
    entry_type = _get_entry_type(log_entry)
    for payload in _generate_payloads(data):
        _send_payload(entry_type, payload)


def _send_payload(entry_type, payload):
    '''
    This function sends the given payload to New Relic Infrastructure's ingest
    service, retrying the request if needed.
    '''
    @http_retryable
    def do_request():
        req = request.Request(_get_ingest_service_url(entry_type), payload)
        req.add_header('X-License-Key', _get_license_key())
        req.add_header('Content-Encoding', 'gzip')
        return request.urlopen(req)

    try:
        response = do_request()
    except MaxRetriesException as e:
        print('Retry limit reached. Failed to send log entry.')
        raise e
    except BadRequestException as e:
        print(e)
    else:
        print('Log entry sent. Response code: {}'.format(response.getcode()))


def _get_license_key():
    return 'f7462ab48bc1fee992a5ab87c707a21b6b1ab893


def _get_ingest_service_url(entity_type):
    '''
    Returns the ingest_service_url.
    This is a concatenation of the HOST + PATH + VERSION
    '''
    path = INGEST_SERVICE_PATHS[entity_type]
    return _get_ingest_service_host() + path + '/' + INGEST_SERVICE_VERSION


def _get_entry_type(log_entry):
    '''
    Returns the EntryType of the entry based on some text found in its value.
    '''
    if '"logGroup":"/aws/vpc/flow-logs"' in log_entry:
        return EntryType.VPC
    elif '"logGroup":"/aws/lambda/' in log_entry and ',\\"NR_LAMBDA_MONITORING\\",' in log_entry:
        return EntryType.LAMBDA
    else:
        return EntryType.OTHER


def _get_ingest_service_host():
    '''
    Environment variable NR_REGION specifies the region for the ingest service.
    Values US and EU return the default ingestion HOST for that region, but any
    URL can be passed.
    Default value is calculated based on the license key.
    '''
    license = os.getenv('LICENSE_KEY', '')
    license_region = 'EU' if license.startswith('eu') else 'US'

    env_ingest_region = os.getenv('NR_REGION', license_region)

    if env_ingest_region == 'US':
        return US_INGEST_SERVICE_HOST
    elif env_ingest_region == 'EU':
        return EU_INGEST_SERVICE_HOST
    else:
        return env_ingest_region


def _generate_payloads(data):
    '''
    Return a list of payloads to be sent to New Relig ingest service.
    This methos usually returns a list of one element, but can be bigger if the
    payload size is too big
    '''
    payload = gzip.compress(json.dumps(data).encode())

    if (len(payload) < MAX_PAYLOAD_SIZE):
        return [payload]

    split_data = _split(data)
    return _generate_payloads(split_data[0]) + _generate_payloads(split_data[1])


def _split(data):
    '''
    When data size is bigger than supported payload, it is divided in two
    different requests
    '''
    context = data['context']
    entry = json.loads(data['entry'])
    logEvents = entry['logEvents']
    half = len(logEvents) // 2

    return [
        _reconstruct_data(context, entry, logEvents[:half]),
        _reconstruct_data(context, entry, logEvents[half:])
    ]


def _reconstruct_data(context, entry, logEvents):
    entry['logEvents'] = logEvents
    return {
        'context': context,
        'entry': json.dumps(entry)
    }


def lambda_handler(event, context):
    '''
    This is the Lambda handler, which is called when the function is invoked.
    Changing the name of this function will require changes in Lambda
    function's configuration.
    '''
    log_type = _get_log_type(event)

    if log_type == 'cw_logs':
        # CloudWatch Log entries are compressed and encoded in Base64
        event_data = b64decode(event['awslogs']['data'])
        log_entry = gzip.decompress(event_data).decode('utf-8')
        _send_log_entry(log_entry, context)

    elif log_type == 's3':
        bucket = event['Records'][0]['s3']['bucket']['name']
        key = event['Records'][0]['s3']['object']['key']

        data = _get_s3_data(bucket, key)

        # There are many log entries in a log file, so send them one by one
        for log_line in data.splitlines():
            _send_log_entry(log_line, context)

    else:
        print('Not supported')
        print(event)

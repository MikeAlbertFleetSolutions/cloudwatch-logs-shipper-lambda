import gzip
import json
import logging
import os
import re

from shipper import LogzioShipper
from StringIO import StringIO

# set logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _extract_aws_logs_data(event):
    # type: (dict) -> dict
    try:
        logs_data_decoded = event['awslogs']['data'].decode('base64')
        logs_data_unzipped = gzip.GzipFile(fileobj=StringIO(logs_data_decoded)).read()
        logs_data_dict = json.loads(logs_data_unzipped)
        return logs_data_dict
    except ValueError as e:
        logger.error("Got exception while loading json, message: {}".format(e))
        raise ValueError("Exception: json loads")


def _parse_cloudwatch_log(log, aws_logs_data, log_type):
    # type: (dict, dict) -> None
    if '@timestamp' not in log:
        log['@timestamp'] = str(log['timestamp'])
        del log['timestamp']

    log['message'] = log['message'].replace('\n', '')

    # remove extra stuff: timestamp, tabs, request id label
    log['message'] = re.sub(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}.[0-9]*Z\s+', '', log['message']).replace("\t", ' ').replace('RequestId: ', '')

    # tag with aws requestid
    requestid = re.search(r'([a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12})', log['message'], re.IGNORECASE)
    if requestid:
        log['requestid'] = requestid.group(1)

    # tag with metrics
    metrics = re.search(r'Duration:\s+([0-9.]+)\s+ms\s+Billed\s+Duration:\s+([0-9]+)\s+ms\s+Memory\s+Size:\s+([0-9]+)\s+MB\s+Max\s+Memory\s+Used:\s+([0-9]+)\s+MB', log['message'], re.IGNORECASE)
    if metrics:
        log['duration'] = metrics.group(1)
        log['billed_duration'] = metrics.group(2)
        log['memory_size'] = metrics.group(3)
        log['memory_used'] = metrics.group(4)

    # normalize START, END, and REPORT entries
    elements = re.search(r'(START|END|REPORT)\s+([a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12})\s*(.*)', log['message'], re.IGNORECASE)
    if elements:
        log['message'] = "{1} {0} {2}".format(elements.group(1), elements.group(2), elements.group(3))

    log['logStream'] = aws_logs_data['logStream']
    log['messageType'] = aws_logs_data['messageType']
    log['owner'] = aws_logs_data['owner']
    log['logGroup'] = aws_logs_data['logGroup']
    log['function_version'] = aws_logs_data['function_version']
    log['invoked_function_arn'] = aws_logs_data['invoked_function_arn']
    log['type'] = log_type

    # If FORMAT is json treat message as a json
    try:
        if os.environ['FORMAT'].lower() == 'json':
            json_object = json.loads(log['message'])
            for key, value in json_object.items():
                log[key] = value
    except (KeyError, ValueError):
        pass


def _enrich_logs_data(aws_logs_data, context):
    # type: (dict, 'LambdaContext') -> None
    try:
        aws_logs_data['function_version'] = context.function_version
        aws_logs_data['invoked_function_arn'] = context.invoked_function_arn
    except KeyError:
        pass


def lambda_handler(event, context):
    # type: (dict, 'LambdaContext') -> None
    try:
        logzio_url = "{0}/?token={1}".format(os.environ['URL'], os.environ['TOKEN'])
        log_type = (os.environ['TYPE'])
    except KeyError as e:
        logger.error("Missing one of the environment variable: {}".format(e))
        raise

    aws_logs_data = _extract_aws_logs_data(event)
    _enrich_logs_data(aws_logs_data, context)
    shipper = LogzioShipper(logzio_url)

    logger.info("About to send {} logs".format(len(aws_logs_data['logEvents'])))
    for log in aws_logs_data['logEvents']:
        if not isinstance(log, dict):
            raise TypeError("Expected log inside logEvents to be a dict but found another type")

        _parse_cloudwatch_log(log, aws_logs_data, log_type)
        shipper.add(log)

    shipper.flush()

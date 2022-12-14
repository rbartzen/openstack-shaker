# Copyright (c) 2015 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import tempfile
import time

from heatclient import exc
from heatclient.common import template_utils
from oslo_log import log as logging
from timeout_decorator import TimeoutError, timeout

LOG = logging.getLogger(__name__)


def create_stack(heat_client, stack_name, template, parameters,
                 environment=None):
    # process_template_path expects a path to a file object, create
    # a temporary named file and write the contents of template to it
    with tempfile.NamedTemporaryFile(mode='w+t') as fd:
        fd.write(template)
        fd.seek(0)
        files, processed_template = template_utils.process_template_path(
            fd.name)

    stack_params = {
        'stack_name': stack_name,
        'template': processed_template,
        'parameters': parameters,
        'environment': environment,
        'files': files,
    }

    stack = heat_client.stacks.create(**stack_params)['stack']
    LOG.info('New stack: %s', stack)

    wait_stack_completion(heat_client, stack['id'])

    return stack['id']


def get_stack_status(heat_client, stack_id):
    # stack.get operation may take long time and run out of time. The reason
    # is that it resolves all outputs which is done serially. On the other hand
    # stack status can be retrieved from the list operation. Internally listing
    # supports paging and every request should not take too long.
    for stack in heat_client.stacks.list():
        if stack.id == stack_id:
            return stack.status, stack.stack_status_reason
    raise exc.HTTPNotFound(message='Stack %s is not found' % stack_id)


def get_id_with_name(heat_client, stack_name):
    # This method isn't really necessary since the Heat client accepts
    # stack_id and stack_name interchangeably. This is provided more as a
    # safety net to use ids which are guaranteed to be unique and provides
    # the benefit of keeping the Shaker code consistent and more easily
    # traceable.
    stack = heat_client.stacks.get(stack_name)
    return stack.id


def wait_stack_completion(heat_client, stack_id):
    reason = None
    status = None

    while True:
        status, reason = get_stack_status(heat_client, stack_id)
        LOG.debug('Stack status: %s', status)
        if status not in ['IN_PROGRESS', '']:
            break

        time.sleep(5)

    if status != 'COMPLETE':
        resources = heat_client.resources.list(stack_id)
        for res in resources:
            if (res.resource_status != 'CREATE_COMPLETE' and
                    res.resource_status_reason):
                LOG.error('Heat stack resource %(res)s of type %(type)s '
                          'failed with %(reason)s',
                          dict(res=res.logical_resource_id,
                               type=res.resource_type,
                               reason=res.resource_status_reason))

        raise exc.StackFailure(stack_id, status, reason)


# set the timeout for this method so we don't get stuck polling indefinitely
# waiting for a delete
@timeout(600)
def wait_stack_deletion(heat_client, stack_id):
    try:
        heat_client.stacks.delete(stack_id)
        while True:
            status, reason = get_stack_status(heat_client, stack_id)
            LOG.debug('Stack status: %s Stack reason: %s', status, reason)
            if status == 'FAILED':
                raise exc.StackFailure('Failed to delete stack %s' % stack_id)

            time.sleep(5)

    except TimeoutError:
        LOG.error('Timed out waiting for deletion of stack %s' % stack_id)

    except exc.HTTPNotFound:
        # once the stack is gone we can assume it was successfully deleted
        # clear the exception so it doesn't confuse the logs
        if sys.version_info < (3, 0):
            sys.exc_clear()
        LOG.info('Stack %s was successfully deleted', stack_id)


def get_stack_outputs(heat_client, stack_id):
    # try to use optimized way to retrieve outputs, fallback otherwise
    if hasattr(heat_client.stacks, 'output_list'):
        try:
            output_list = heat_client.stacks.output_list(stack_id)['outputs']

            result = {}
            for output in output_list:
                output_key = output['output_key']
                value = heat_client.stacks.output_show(stack_id, output_key)
                result[output_key] = value['output']['output_value']

            return result
        except Exception as e:
            LOG.info('Cannot get output list, fallback to old way: %s', e)

    outputs_list = heat_client.stacks.get(stack_id).to_dict()['outputs']
    return dict((item['output_key'], item['output_value'])
                for item in outputs_list)

import io
import json
import os
import requests
import hashlib
import logging
import urllib
import numpy as np
import wave
import shutil
import argparse
import re
import datetime
import pandas as pd

from copy import deepcopy
from operator import itemgetter
from PIL import Image
from urllib.parse import urlparse
from nltk.tokenize.treebank import TreebankWordTokenizer
from lxml import etree
from collections import defaultdict
from label_studio_tools.core.utils.params import get_env

logger = logging.getLogger(__name__)

_LABEL_TAGS = {'Label', 'Choice'}
_NOT_CONTROL_TAGS = {
    'Filter',
}
LOCAL_FILES_DOCUMENT_ROOT = get_env(
    'LOCAL_FILES_DOCUMENT_ROOT', default=os.path.abspath(os.sep)
)

TreebankWordTokenizer.PUNCTUATION = [
    (re.compile(r"([:,])([^\d])"), r" \1 \2"),
    (re.compile(r"([:,])$"), r" \1 "),
    (re.compile(r"\.\.\."), r" ... "),
    (re.compile(r"[;@#$/%&]"), r" \g<0> "),
    (
        re.compile(r'([^\.])(\.)([\]\)}>"\']*)\s*$'),
        r"\1 \2\3 ",
    ),  # Handles the final period.
    (re.compile(r"[?!]"), r" \g<0> "),
    (re.compile(r"([^'])' "), r"\1 ' "),
]


class ExpandFullPath(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, os.path.abspath(os.path.expanduser(values)))


def tokenize(text):
    tok_start = 0
    out = []
    for tok in text.split():
        if len(tok):
            out.append((tok, tok_start))
            tok_start += len(tok) + 1
        else:
            tok_start += 1
    return out


def create_tokens_and_tags(text, spans):
    # tokens_and_idx = tokenize(text) # This function doesn't work properly if text contains multiple whitespaces...
    token_index_tuples = [
        token for token in TreebankWordTokenizer().span_tokenize(text)
    ]
    tokens_and_idx = [(text[start:end], start) for start, end in token_index_tuples]
    if spans and all(
        [
            span.get('start') is not None and span.get('end') is not None
            for span in spans
        ]
    ):
        spans = list(sorted(spans, key=itemgetter('start')))
        span = spans.pop(0)
        span_start = span['start']
        span_end = span['end'] - 1
        prefix = 'B-'
        tokens, tags = [], []
        for token, token_start in tokens_and_idx:
            tokens.append(token)
            token_end = (
                token_start + len(token) - 1
            )  # "- 1" - This substraction is wrong. token already uses the index E.g. "Hello" is 0-4
            token_start_ind = token_start  # It seems like the token start is too early.. for whichever reason

            # if for some reason end of span is missed.. pop the new span (Which is quite probable due to this method)
            # Attention it seems like span['end'] is the index of first char afterwards. In case the whitespace is part of the
            # labell we need to subtract one. Otherwise next token won't trigger the span update.. only the token after next..
            if token_start_ind > span_end:
                while spans:
                    span = spans.pop(0)
                    span_start = span['start']
                    span_end = span['end'] - 1
                    prefix = 'B-'
                    if token_start <= span_end:
                        break
            # Add tag "O" for spans that:
            # - are empty
            # - span start has passed over token_end
            # - do not have any label (None or empty list)
            if not span or token_end < span_start or not span.get('labels'):
                tags.append('O')
            elif span_start <= token_end and span_end >= token_start_ind:
                tags.append(prefix + span['labels'][0])
                prefix = 'I-'
            else:
                tags.append('O')
    else:
        tokens = [token for token, _ in tokens_and_idx]
        tags = ['O'] * len(tokens)

    return tokens, tags


def _get_upload_dir(project_dir=None, upload_dir=None):
    """Return either upload_dir, or path by LS_UPLOAD_DIR, or project_dir/upload"""
    if upload_dir:
        return upload_dir
    upload_dir = os.environ.get('LS_UPLOAD_DIR')
    if not upload_dir and project_dir:
        upload_dir = os.path.join(project_dir, 'upload')
        if not os.path.exists(upload_dir):
            upload_dir = None
    if not upload_dir:
        raise FileNotFoundError(
            "Can't find upload dir: either LS_UPLOAD_DIR or project should be passed to converter"
        )
    return upload_dir


def download(
    url,
    output_dir,
    filename=None,
    project_dir=None,
    return_relative_path=False,
    upload_dir=None,
    download_resources=True,
):
    is_local_file = url.startswith('/data/') and '?d=' in url
    is_uploaded_file = url.startswith('/data/upload')

    if is_uploaded_file:
        upload_dir = _get_upload_dir(project_dir, upload_dir)
        filename = urllib.parse.unquote(url.replace('/data/upload/', ''))
        filepath = os.path.join(upload_dir, filename)
        logger.debug(
            f'Copy {filepath} to {output_dir}'.format(
                filepath=filepath, output_dir=output_dir
            )
        )
        if download_resources:
            shutil.copy(filepath, output_dir)
        if return_relative_path:
            return os.path.join(
                os.path.basename(output_dir), os.path.basename(filename)
            )
        return filepath

    if is_local_file:
        filename, dir_path = url.split('/data/', 1)[-1].split('?d=')
        dir_path = str(urllib.parse.unquote(dir_path))
        filepath = os.path.join(LOCAL_FILES_DOCUMENT_ROOT, dir_path)
        if not os.path.exists(filepath):
            raise FileNotFoundError(filepath)
        if download_resources:
            shutil.copy(filepath, output_dir)
        return filepath

    if filename is None:
        basename, ext = os.path.splitext(os.path.basename(urlparse(url).path))
        filename = f'{basename}{ext}'
        filepath = os.path.join(output_dir, filename)
        if os.path.exists(filepath):
            filename = (
                basename
                + '_'
                + hashlib.md5(
                    url.encode() + str(datetime.datetime.now().timestamp()).encode()
                ).hexdigest()[:4]
                + ext
            )

    filepath = os.path.join(output_dir, filename)
    if not os.path.exists(filepath):
        logger.info('Download {url} to {filepath}'.format(url=url, filepath=filepath))
        if download_resources:
            r = requests.get(url)
            r.raise_for_status()
            with io.open(filepath, mode='wb') as fout:
                fout.write(r.content)
    if return_relative_path:
        return os.path.join(os.path.basename(output_dir), os.path.basename(filename))
    return filepath


def get_image_size(image_path):
    return Image.open(image_path).size


def get_image_size_and_channels(image_path):
    i = Image.open(image_path)
    w, h = i.size
    c = len(i.getbands())
    return w, h, c


def get_audio_duration(audio_path):
    with wave.open(audio_path, mode='r') as f:
        return f.getnframes() / float(f.getframerate())


def ensure_dir(dir_path):
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)


def parse_config(config_string):
    """
    :param config_string: Label config string
    :return: structured config of the form:
    {
        "<ControlTag>.name": {
            "type": "ControlTag",
            "to_name": ["<ObjectTag1>.name", "<ObjectTag2>.name"],
            "inputs: [
                {"type": "ObjectTag1", "value": "<ObjectTag1>.value"},
                {"type": "ObjectTag2", "value": "<ObjectTag2>.value"}
            ],
            "labels": ["Label1", "Label2", "Label3"] // taken from "alias" if exists or "value"
    }
    """
    if not config_string:
        return {}

    def _is_input_tag(tag):
        return tag.attrib.get('name') and tag.attrib.get('value')

    def _is_output_tag(tag):
        return (
            tag.attrib.get('name')
            and tag.attrib.get('toName')
            and tag.tag not in _NOT_CONTROL_TAGS
        )

    def _get_parent_output_tag_name(tag, outputs):
        # Find parental <Choices> tag for nested tags like <Choices><View><View><Choice>...
        parent = tag
        while True:
            parent = parent.getparent()
            if parent is None:
                return
            name = parent.attrib.get('name')
            if name in outputs:
                return name

    try:
        xml_tree = etree.fromstring(config_string)
    except etree.XMLSyntaxError as e:
        raise ValueError(str(e))

    inputs, outputs, labels = {}, {}, defaultdict(dict)
    for tag in xml_tree.iter():
        if _is_output_tag(tag):
            tag_info = {'type': tag.tag, 'to_name': tag.attrib['toName'].split(',')}
            # Grab conditionals if any
            conditionals = {}
            if tag.attrib.get('perRegion') == 'true':
                if tag.attrib.get('whenTagName'):
                    conditionals = {'type': 'tag', 'name': tag.attrib['whenTagName']}
                elif tag.attrib.get('whenLabelValue'):
                    conditionals = {
                        'type': 'label',
                        'name': tag.attrib['whenLabelValue'],
                    }
                elif tag.attrib.get('whenChoiceValue'):
                    conditionals = {
                        'type': 'choice',
                        'name': tag.attrib['whenChoiceValue'],
                    }
            if conditionals:
                tag_info['conditionals'] = conditionals
            outputs[tag.attrib['name']] = tag_info
        elif _is_input_tag(tag):
            inputs[tag.attrib['name']] = {
                'type': tag.tag,
                'value': tag.attrib['value'].lstrip('$'),
            }
        if tag.tag not in _LABEL_TAGS:
            continue
        parent_name = _get_parent_output_tag_name(tag, outputs)
        if parent_name is not None:
            actual_value = tag.attrib.get('alias') or tag.attrib.get('value')
            if not actual_value:
                logger.debug(
                    'Inspecting tag {tag_name}... found no "value" or "alias" attributes.'.format(
                        tag_name=etree.tostring(tag, encoding='unicode').strip()[:50]
                    )
                )
            else:
                labels[parent_name][actual_value] = dict(tag.attrib)
    for output_tag, tag_info in outputs.items():
        tag_info['inputs'] = []
        for input_tag_name in tag_info['to_name']:
            if input_tag_name not in inputs:
                logger.debug(
                    f'to_name={input_tag_name} is specified for output tag name={output_tag}, '
                    'but we can\'t find it among input tags'
                )
                continue
            tag_info['inputs'].append(inputs[input_tag_name])
        tag_info['labels'] = list(labels[output_tag])
        tag_info['labels_attrs'] = labels[output_tag]
    return outputs


def get_polygon_area(x, y):
    """https://en.wikipedia.org/wiki/Shoelace_formula"""

    assert len(x) == len(y)

    return float(0.5 * np.abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))))


def get_polygon_bounding_box(x, y):
    assert len(x) == len(y)

    x1, y1, x2, y2 = min(x), min(y), max(x), max(y)
    return [x1, y1, x2 - x1, y2 - y1]


def get_annotator(item, default=None, int_id=False):
    """Get annotator id or email from annotation"""
    annotator = item['completed_by']
    if isinstance(annotator, dict):
        annotator = annotator.get('email', default)
        return annotator

    if isinstance(annotator, int) and int_id:
        return annotator

    return str(annotator)


def get_json_root_type(filename):
    char = 'x'
    with open(filename, "r", encoding='utf-8') as f:
        # Read the file character by character
        while char != '':
            char = f.read(1)

            # Skip any whitespace
            if char.isspace():
                continue

            # If the first non-whitespace character is '{', it's a dict
            if char == '{':
                return "dict"

            # If the first non-whitespace character is '[', it's an array
            if char == '[':
                return "list"

            # If neither, the JSON file is invalid
            return "invalid"

    # If the file is empty, return "empty"
    return "empty"


def prettify_result(v):
    """
    :param v: list of regions or results
    :return: label name as is if there is only 1 item in result `v`, else list of label names
    """
    out = []
    tag_type = None
    for i in v:
        j = deepcopy(i)
        tag_type = j.pop('type')
        if tag_type == 'Choices' and len(j['choices']) == 1:
            out.append(j['choices'][0])
        elif tag_type == 'TextArea' and len(j['text']) == 1:
            out.append(j['text'][0])
        else:
            out.append(j)
    return out[0] if tag_type in ('Choices', 'TextArea') and len(out) == 1 else out


def get_filename(annotation_item):
    annotation_id = annotation_item['id']
    annotation_file_upload = annotation_item['file_upload']
    return f"export-{annotation_id}-{annotation_file_upload}.csv"

def process_upwatch_annotation(annotation_item, output_dir):
    export_filename = get_filename(annotation_item)

    csv_path = annotation_item['data']['csv_path']
    csv_path_2 = annotation_item['data']['csv_path_2']

    # Read signal of 2 sensors
    signal_data = pd.read_csv(csv_path)
    signal_data_2 = pd.read_csv(csv_path_2)

    # Calculate the ax3 array from its components in both sensors
    signal_data['ax3_butterworth'] = signal_data[[f'ax3_{idx}' for idx in range(5)]].apply(lambda row: row.values.tolist(), axis=1)
    signal_data_2['ax3_butterworth'] = signal_data_2[[f'ax3_{idx}' for idx in range(5)]].apply(lambda row: row.values.tolist(), axis=1)

    with open(export_filename, 'w') as fout:
        # Only using first user annotation
        annotation_result = annotation_item['annotations'][0]['result']

        events, sensor_numbers, sensor_starts, sensor_ends, butterworth_arrays, bandpass_arrays, meta_text_array = [], [], [], [], [], [], []
        for result_item in annotation_result:
            sensor_number = annotation_item['data']['sensor']
            if str(sensor_number) != '1': continue # Skipping data from other sensors than 1

            # Add sensor 1 data
            events.append(result_item['value']['timeserieslabels'])
            sensor_numbers.append(sensor_number)

            start_index, end_index = result_item['value']['start'], result_item['value']['end']
            sensor_starts.append(start_index)
            sensor_ends.append(end_index)

            butterworth_arrays.append(list(signal_data['ax3_butterworth'][start_index:end_index]))
            bandpass_arrays.append(list(signal_data['ax3_bandpass'][start_index:end_index]))


            # Add sensor 2 data
            events.append(result_item['value']['timeserieslabels'])
            sensor_numbers.append(2)

            start_index, end_index = result_item['value']['start'], result_item['value']['end']
            sensor_starts.append(start_index)
            sensor_ends.append(end_index)

            butterworth_arrays.append(list(signal_data_2['ax3_butterworth'][start_index:end_index]))
            bandpass_arrays.append(list(signal_data_2['ax3_bandpass'][start_index:end_index]))

            note = ''
            if 'meta' in result_item and 'text' in result_item['meta']:
                note = list(result_item['meta']['text'])
            meta_text_array.append(note)
            meta_text_array.append(note)
        print("Creating dataframe")

        df = pd.DataFrame({
            'events': events,
            'sensor_numbers': sensor_numbers,
            'sensor_starts': sensor_starts,
            'sensor_ends': sensor_ends,
            'ax3_butterworth': butterworth_arrays,
            'ax3_bandpass': bandpass_arrays,
            'meta_text_array': meta_text_array
        })
        print('Saving result to', output_dir)
        os.makedirs(output_dir, exist_ok=True)
        df.to_csv(os.path.join(output_dir, export_filename), index=False)

def process_upwatch_data(input_data, output_dir):
    with open(input_data) as fin:
        annotation_data = json.load(fin)
        for annotation_item in annotation_data:
            process_upwatch_annotation(annotation_item, output_dir)
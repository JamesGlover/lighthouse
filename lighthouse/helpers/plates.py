import logging
import re
from http import HTTPStatus
from typing import Any, Dict, List, Optional

import requests
from flask import current_app as app

from lighthouse.constants import FIELD_COG_BARCODE
from lighthouse.exceptions import (
    DataError,
    MissingCentreError,
    MissingSourceError,
    MultipleCentresError,
)

logger = logging.getLogger(__name__)


def add_cog_barcodes(samples: List[Dict[str, str]]) -> List[Dict[str, str]]:

    centre_name = confirm_cente(samples)
    centre_prefix = get_centre_prefix(centre_name)
    num_samples = len(samples)

    logger.info(f"Getting COG-UK barcodes for {num_samples} samples")

    baracoda_url = (
        f"http://{app.config['BARACODA_URL']}"
        f"/barcodes_group/{centre_prefix}/new?count={num_samples}"
    )
    try:
        response = requests.post(baracoda_url)
        if response.status_code == HTTPStatus.CREATED:
            barcodes = response.json()["barcodes_group"]["barcodes"]
            for (sample, barcode) in zip(samples, barcodes):
                sample[FIELD_COG_BARCODE] = barcode
        else:
            raise Exception("Unable to create COG barcodes")
    except requests.ConnectionError:
        raise requests.ConnectionError("Unable to access baracoda")

    return samples


def get_centre_prefix(centre_name: str) -> Optional[str]:
    logger.debug(f"Getting the prefix for '{centre_name}'")
    try:
        #  get the centre collection
        centres = app.data.driver.db.centres

        # use a case insensitive search for the centre name
        filter = {"name": {"$regex": f"^(?i){centre_name}$"}}

        assert centres.count_documents(filter) == 1

        centre = centres.find_one(filter)

        prefix = centre["prefix"]

        logger.debug(f"Prefix for '{centre_name}' is '{prefix}")

        return prefix
    except Exception as e:
        logger.exception(e)
        return None
    except AssertionError as e:
        logger.exception(e)
        raise DataError("Multiple centres with the same name")


def get_samples(plate_barcode: str) -> Optional[List[Dict[str, Any]]]:
    logger.info(f"Getting all samples for {plate_barcode}")

    samples = app.data.driver.db.samples

    samples_for_barcode = list(samples.find({"plate_barcode": plate_barcode}))

    logger.info(f"Found {len(samples_for_barcode)} samples for {plate_barcode}")

    return samples_for_barcode


def confirm_cente(samples: List[Dict[str, str]]) -> str:
    """Confirm that the centre for all the samples is populated and the same and return the centre
    name

    Arguments:
        samples {List} -- the list of samples to check

    Returns:
        str -- the name of the centre for these samples
    """
    logger.debug("confirm_cente()")

    try:
        # check that the 'source' field has a valid name
        for sample in samples:
            if not sample["source"]:
                raise MissingCentreError(sample)

        # create a set from the 'source' field to check we only have 1 unique centre for these
        #   samples
        centre_set = {sample["source"] for sample in samples}
    except KeyError:
        raise MissingSourceError()
    else:
        if len(centre_set) > 1:
            raise MultipleCentresError()

    return centre_set.pop()


def create_post_body(barcode: str, samples: List[Dict[str, str]]) -> Dict[str, Any]:
    logger.debug(f"Creating POST body to send to SS for barcode '{barcode}'")

    phenotype_pattern = re.compile(r"^Result$", re.I)
    description_pattern = re.compile(r"^Root Sample ID$", re.I)
    wells_content = {}
    for sample in samples:
        for key, value in sample.items():
            if phenotype_pattern.match(key.strip()):
                phenotype = value

            if description_pattern.match(key.strip()):
                description = value

        assert phenotype is not None
        assert sample[FIELD_COG_BARCODE] is not None

        well = {
            "content": {
                "phenotype": phenotype.strip().lower(),
                "supplier_name": sample[FIELD_COG_BARCODE],
                "sample_description": description,
            }
        }
        wells_content[sample["coordinate"]] = well

    body = {
        "barcode": barcode,
        "purpose_uuid": app.config["SS_UUID_PLATE_PURPOSE"],
        "study_uuid": app.config["SS_UUID_STUDY"],
        "wells": wells_content,
    }

    return {"data": {"type": "plates", "attributes": body}}


def send_to_ss(body: Dict[str, Any]) -> requests.Response:
    ss_url = f"http://{app.config['SS_HOST']}/api/v2/heron/plates"

    logger.info(f"Sending {body} to {ss_url}")

    headers = {"X-Sequencescape-Client-Id": app.config["SS_API_KEY"]}

    try:
        response = requests.post(ss_url, json=body, headers=headers)
        logger.debug(response.status_code)
    except requests.ConnectionError:
        raise requests.ConnectionError("Unable to access SS")

    return response
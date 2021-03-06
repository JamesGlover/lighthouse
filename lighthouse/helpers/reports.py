import logging
import math
import os
import pathlib
import re
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Dict, List, Tuple

import pandas as pd  # type: ignore
import requests
import sqlalchemy  # type: ignore
from flask import current_app as app
from lighthouse.constants import (
    FIELD_COORDINATE,
    FIELD_DATE_TESTED,
    FIELD_PLATE_BARCODE,
    FIELD_RESULT,
    FIELD_ROOT_SAMPLE_ID,
    FIELD_SOURCE,
    STAGE_MATCH_POSITIVE,
)
from lighthouse.exceptions import ReportCreationError
from lighthouse.utils import pretty
from pandas import DataFrame
from pymongo.collection import Collection  # type: ignore

logger = logging.getLogger(__name__)
PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent


def get_reports_details(filename: str = None) -> List[Dict[str, str]]:
    """Get the details of reports, including:
    - size
    - created timestamp
    - download URL - should point to where the files will be hosted from

    Keyword Arguments:
        filename {str} -- if filename is provided, only this report's details will be returned
        (default: {None})

    Returns:
        List[Dict[str, str]] -- list of report details
    """
    logger.debug("Getting reports' details")

    REPORTS_PATH = PROJECT_ROOT.joinpath(app.config["REPORTS_DIR"])

    if filename:
        reports = iter([filename])
    else:
        (_, _, files) = next(os.walk(REPORTS_PATH))

        # we only want files which match the report naming convention
        report_pattern = re.compile(r"^\d{6}_\d{4}_positives_with_locations.xlsx$")
        reports = filter(report_pattern.match, files)

    return [
        {
            "filename": filename,
            "size": get_file_size(REPORTS_PATH.joinpath(filename)),
            "created": datetime.fromtimestamp(
                os.path.getmtime(REPORTS_PATH.joinpath(filename))
            ).strftime("%c"),
            "download_url": f"{app.config['DOWNLOAD_REPORTS_URL']}/{filename}",
        }
        for filename in reports
    ]


def get_file_size(file_path: pathlib.PurePath) -> str:
    """Get the size of a file in a human friendly format.

    Arguments:
        file_path {pathlib.PurePath} -- path to the file in question

    Returns:
        str -- file size in human friendly format
    """
    size_in_bytes = os.path.getsize(file_path)

    return convert_size(size_in_bytes)


def convert_size(size_in_bytes: int) -> str:
    """Converts the size of a file (in bytes) to a human friendly format.

    Based on: https://stackoverflow.com/a/14822210

    Arguments:
        size_in_bytes {int} -- size of file in bytes

    Returns:
        str -- file size in human friendly format
    """
    if size_in_bytes == 0:
        return "0B"

    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_in_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_in_bytes / p, 2)

    return f"{s} {size_name[i]}"


def get_new_report_name_and_path() -> Tuple[str, pathlib.PurePath]:
    """Get the name and path of a report which is being created.

    Returns:
        Tuple[str, pathlib.PurePath] -- filename and path of report being created
    """
    report_date = datetime.now().strftime("%y%m%d_%H%M")
    report_name = f"{report_date}_positives_with_locations.xlsx"
    REPORTS_PATH = PROJECT_ROOT.joinpath(app.config["REPORTS_DIR"])
    report_path = REPORTS_PATH.joinpath(report_name)

    return report_name, report_path


# Strip any leading zeros from the coordinate
# eg. A01 => A1
def unpad_coordinate(coordinate):
    return (
        re.sub(r"0(\d+)$", r"\1", coordinate)
        if (coordinate and isinstance(coordinate, str))
        else coordinate
    )


def delete_reports(filenames):
    """delete reports from the standard reports folder if they exist.

    Returns:
        Nothing.
    """
    for filename in filenames:
        full_path = f"{app.config['REPORTS_DIR']}/{filename}"
        if os.path.isfile(full_path):
            os.remove(full_path)


def map_labware_to_location(labware_barcodes):
    response = get_locations_from_labwhere(labware_barcodes)

    if response.status_code != HTTPStatus.OK:
        raise ReportCreationError("Response from LabWhere is not OK")

    # create a plate_barcode to location_barcode mapping to join with samples
    # return none for samples where location barcode is not present
    labware_to_location_barcode = [
        {
            FIELD_PLATE_BARCODE: record["barcode"],
            "location_barcode": str(record["location_barcode"] or ""),
        }
        for record in response.json()
    ]

    labware_to_location_barcode_df = pd.DataFrame.from_records(labware_to_location_barcode)
    logger.info(f"{len(labware_to_location_barcode_df.index)} locations for plate barcodes found")
    pretty(logger, labware_to_location_barcode_df)

    return labware_to_location_barcode_df


def get_locations_from_labwhere(labware_barcodes):
    """
    Example record from labwhere:
    { 'barcode': 'GLA001024R', 'location_barcode': 'lw-uk-biocentre-box-gsw--98-14813'}
    """

    return requests.post(
        f"http://{app.config['LABWHERE_URL']}/api/labwares_by_barcode",
        json={"barcodes": labware_barcodes},
    )


def get_cherrypicked_samples(root_sample_ids, plate_barcodes, chunk_size=50000):
    # Find which samples have been cherrypicked using MLWH & Events warehouse
    # Returns dataframe with 4 columns: those needed to uniquely identify the sample
    # resulting dataframe only contains those samples that have been cherrypicked
    # (= those that have an entry for the relevant event type in the event warehouse)
    # TODO: move into external method.

    try:
        # Create an empty DataFrame to merge into
        concat_frame = pd.DataFrame()

        chunk_root_sample_ids = [
            root_sample_ids[x : (x + chunk_size)]  # noqa: E203
            for x in range(0, len(root_sample_ids), chunk_size)
        ]

        sql_engine = sqlalchemy.create_engine(
            f"mysql+pymysql://{app.config['WAREHOUSES_RO_CONN_STRING']}", pool_recycle=3600
        )
        db_connection = sql_engine.connect()

        ml_wh_db = app.config["ML_WH_DB"]
        events_wh_db = app.config["EVENTS_WH_DB"]

        for chunk_root_sample_id in chunk_root_sample_ids:
            sql = (
                f"select mlwh_sample.description as `{FIELD_ROOT_SAMPLE_ID}`, mlwh_stock_resource.labware_human_barcode as `{FIELD_PLATE_BARCODE}`"  # noqa: E501
                f",mlwh_sample.phenotype as `Result_lower`, mlwh_stock_resource.labware_coordinate as `{FIELD_COORDINATE}`"  # noqa: E501
                f" FROM {ml_wh_db}.sample as mlwh_sample"
                f" JOIN {ml_wh_db}.stock_resource mlwh_stock_resource ON (mlwh_sample.id_sample_tmp = mlwh_stock_resource.id_sample_tmp)"  # noqa: E501
                f" JOIN {events_wh_db}.subjects mlwh_events_subjects ON (mlwh_events_subjects.friendly_name = sanger_sample_id)"  # noqa: E501
                f" JOIN {events_wh_db}.roles mlwh_events_roles ON (mlwh_events_roles.subject_id = mlwh_events_subjects.id)"  # noqa: E501
                f" JOIN {events_wh_db}.events mlwh_events_events ON (mlwh_events_roles.event_id = mlwh_events_events.id)"  # noqa: E501
                f" JOIN {events_wh_db}.event_types mlwh_events_event_types ON (mlwh_events_events.event_type_id = mlwh_events_event_types.id)"  # noqa: E501
                f" WHERE mlwh_sample.description IN %(root_sample_ids)s"
                f" AND mlwh_stock_resource.labware_human_barcode IN %(plate_barcodes)s"
                " AND mlwh_events_event_types.key = 'cherrypick_layout_set'"
                " GROUP BY mlwh_sample.description, mlwh_stock_resource.labware_human_barcode, mlwh_sample.phenotype, mlwh_stock_resource.labware_coordinate"  # noqa: E501
            )

            frame = pd.read_sql(
                sql,
                db_connection,
                params={
                    "root_sample_ids": tuple(chunk_root_sample_id),
                    "plate_barcodes": tuple(plate_barcodes),
                },
            )

            # drop_duplicates is needed because the same 'root sample id' could pop up in two
            # different batches, and then it would retrieve the same rows for that root sample id
            # twice do reset_index after dropping duplicates to make sure the rows are numbered in
            # a way that makes sense
            concat_frame = concat_frame.append(frame).drop_duplicates().reset_index(drop=True)

        return concat_frame
    except Exception as e:
        print("Error while connecting to MySQL", e)
        return None
    finally:
        db_connection.close()


def get_all_positive_samples(samples_collection: Collection) -> DataFrame:
    """Get all the positive samples from mongo from a specific date.

    Args:
        samples_collection (Collection): the samples collection.

    Returns:
        DataFrame: a pandas DataFrame with the positive samples.
    """
    logger.debug("Getting all positive samples from a specific date")

    # The projection defines which fields are present in the documents from the output of the mongo
    # query
    projection = {
        "_id": False,
        FIELD_SOURCE: True,
        FIELD_PLATE_BARCODE: True,
        FIELD_ROOT_SAMPLE_ID: True,
        FIELD_RESULT: True,
        FIELD_DATE_TESTED: True,
        FIELD_COORDINATE: True,
    }

    DATE_EXTRACTED = "extracted_date"
    DATE_CONVERTED = "converted_date"
    DATE_ERROR = "date_conversion_error"

    # The pipeline defines stages which execute in sequence
    pipeline = [
        # 1. First run the positive match stage
        STAGE_MATCH_POSITIVE,
        {
            # 2. We then need to extract the date using substring since most dates look like this:
            # "2020-05-10 07:30:00 UTC" but the `dateFromString` function does not handle the
            # timezone string "UTC"
            "$addFields": {
                DATE_EXTRACTED: {
                    "$substrBytes": [f"${FIELD_DATE_TESTED}", 0, 19],
                },
            },
        },
        {
            # 3. Then add a date field which converts the extracted date string to a mongo date
            # type using the specified format. If it cannot parse the date using the first format
            # it tries with the other format found in the data
            # TODO: handle the case that no format is matched
            "$addFields": {
                DATE_CONVERTED: {
                    "$dateFromString": {
                        "dateString": f"${DATE_EXTRACTED}",
                        "format": "%Y-%m-%d %H:%M:%S",
                        "timezone": "UTC",
                        "onError": {
                            "$dateFromString": {
                                "dateString": f"${DATE_EXTRACTED}",
                                "format": "%d/%m/%Y %H:%M",
                                "timezone": "UTC",
                                "onError": DATE_ERROR,
                            },
                        },
                    },
                },
            },
        },
        # 4. We only want documents which have valid dates that we can compare against
        {"$match": {DATE_CONVERTED: {"$ne": DATE_ERROR}}},
        # 5. Find all the document after a certain date
        {"$match": {DATE_CONVERTED: {"$gte": report_query_window_start()}}},
        # 6. Define which fields to have in the output documents
        {"$project": projection},
    ]

    # Perform an aggregation using the defined pipeline - this will run through the pipeline
    # "stages" in sequence
    results = samples_collection.aggregate(pipeline)

    # converting to a dataframe to make it easy to join with data from labwhere
    positive_samples_df = pd.DataFrame.from_records(results)

    logger.info(f"{len(positive_samples_df.index)} positive samples")
    pretty(logger, positive_samples_df)

    # strip zeros out of the well coordinates
    positive_samples_df[FIELD_COORDINATE] = positive_samples_df[FIELD_COORDINATE].map(
        lambda coord: unpad_coordinate(coord)
    )

    # create 'plate and well' column for copy-pasting into Sequencescape submission, e.g. DN1234:A1
    positive_samples_df["plate and well"] = (
        positive_samples_df[FIELD_PLATE_BARCODE] + ":" + positive_samples_df[FIELD_COORDINATE]
    )

    return positive_samples_df


def add_cherrypicked_column(existing_dataframe):
    root_sample_ids = existing_dataframe[FIELD_ROOT_SAMPLE_ID].to_list()
    plate_barcodes = existing_dataframe["plate_barcode"].unique()

    cherrypicked_samples_df = get_cherrypicked_samples(root_sample_ids, plate_barcodes)
    cherrypicked_samples_df["LIMS submission"] = "Yes"

    logger.info(f"{len(cherrypicked_samples_df.index)} cherrypicked samples")

    # The result value in the phenotype in MLWH.sample is all lowercase,
    # because it is converted in create_post_body in helpers/plates.py,
    # whereas in the original data in MongoDB and MLWH.lighthouse_sample it is capitalised
    existing_dataframe["Result_lower"] = existing_dataframe["Result"].str.lower()

    existing_dataframe = existing_dataframe.merge(
        cherrypicked_samples_df,
        how="left",
        on=[FIELD_ROOT_SAMPLE_ID, FIELD_PLATE_BARCODE, "Result_lower", FIELD_COORDINATE],
    )
    # Fill any empty cells for the column with 'No' (those that do not have cherrypicking events)
    existing_dataframe = existing_dataframe.fillna({"LIMS submission": "No"})

    # remove the extra column we merged on as no longer needed
    existing_dataframe = existing_dataframe.drop(columns=["Result_lower"])

    return existing_dataframe


def get_distinct_plate_barcodes(samples):

    logger.debug("Getting list of distinct plate barcodes")
    # for some reason we have some records (documents in mongo language) where the plate_barcode
    #   is empty so ignore those
    # TODO: abstract into new method
    distinct_plate_barcodes = samples.distinct(
        FIELD_PLATE_BARCODE, {FIELD_PLATE_BARCODE: {"$nin": ["", None]}}
    )
    logger.info(f"{len(distinct_plate_barcodes)} distinct barcodes")

    return distinct_plate_barcodes


def join_samples_declarations(positive_samples):

    samples_declarations = app.data.driver.db.samples_declarations

    # Latest declarations group by root_sample_id
    # Id is needed to control the group aggregation
    # Excel formatter required date without timezone
    declarations = samples_declarations.aggregate(
        [
            {"$sort": {"declared_at": -1}},
            {
                "$group": {
                    "_id": "$root_sample_id",
                    FIELD_ROOT_SAMPLE_ID: {"$first": "$root_sample_id"},
                    "Value In Sequencing": {"$first": "$value_in_sequencing"},
                    "Declared At": {
                        "$first": {
                            "$dateToString": {"date": "$declared_at", "format": "%Y-%m-%dT%H:%M:%S"}
                        }
                    },
                }
            },
            {"$unset": "_id"},
        ]
    )

    declarations_records = [record for record in declarations]

    if len(declarations_records) > 0:
        logger.debug("Joining declarations")
        declarations_frame = pd.DataFrame.from_records(declarations_records)
        results = positive_samples.merge(declarations_frame, how="left", on=FIELD_ROOT_SAMPLE_ID)

        # Give a default value of Unknown to any entry that does not have a
        # sample declaration
        results = results.fillna({"Value In Sequencing": "Unknown"})
        return results

    return positive_samples


def report_query_window_start() -> datetime:
    """Return the start date for the report window.

    Returns:
        datetime: start date for the report window
    """
    window_size = app.config["REPORT_WINDOW_SIZE"]
    logger.debug(f"Current report window size: {window_size}")

    start = datetime.now() + timedelta(days=-window_size)
    logger.info(f"Report starting from: {start.strftime('%d/%m/%Y')}")

    return datetime(year=start.year, month=start.month, day=start.day)

from http import HTTPStatus
from unittest.mock import patch
import json


def test_get_reports_endpoint(client):
    with patch(
        "lighthouse.blueprints.reports.get_reports_details", return_value=[],
    ):
        response = client.get("/reports")
        assert response.status_code == HTTPStatus.OK


def test_get_reports_list(client):
    with patch(
        "lighthouse.blueprints.reports.get_reports_details", return_value=[],
    ):
        response = client.get("/reports")
        assert response.json == {"reports": []}


def test_create_report(client, app, tmp_path, samples, labwhere_samples):
    with app.app_context():
        with patch(
            "lighthouse.jobs.reports.get_new_report_name_and_path",
            return_value=["test.xlsx", f"{tmp_path}/test.xlsx"],
        ):
            with patch(
                "lighthouse.blueprints.reports.get_reports_details",
                return_value="Some details of a report",
            ):
                response = client.post("/reports/new")
                assert response.json == {"reports": "Some details of a report"}


def test_delete_reports_endpoint(client):
    with patch(
        "lighthouse.blueprints.reports.delete_reports", return_value=None,
    ):
        json_body = {
            "data": {
                "filenames": [
                    "200716_1345_positives_with_locations.xlsx",
                    "200716_1618_positives_with_locations.xlsx",
                    "200716_1640_positives_with_locations.xlsx",
                    "200716_1641_positives_with_locations.xlsx",
                    "200716_1642_positives_with_locations.xlsx",
                ]
            }
        }

        response = client.post("delete_reports", json=json.dumps(json_body))

        assert response.status_code == HTTPStatus.OK

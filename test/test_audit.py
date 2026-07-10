from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

CATALOG = b"""
<table>
<tr><th>Course Code</th><th>Title</th><th>Credits</th>
<th>Prerequisites</th><th>Cross-listed</th></tr>
<tr><td>COSC 2006</td><td>PF I</td><td>3</td><td>None</td><td></td></tr>
<tr><td>COSC 2007</td><td>PF II</td><td>3</td><td>COSC-2006</td><td></td></tr>
<tr><td>COSC 3506</td><td>SSD</td><td>3</td><td>COSC 2007</td><td>ITEC 3506</td></tr>
<tr><td>ITEC 3506</td><td>SSD</td><td>3</td><td>COSC 2007</td><td>COSC 3506</td></tr>
</table>
"""


def setup_function():
    app.state.catalog = {}
    app.state.students = {}


def load_catalog():
    response = client.post(
        "/api/v1/admin/catalog/import",
        files={"file": ("catalog.html", CATALOG, "text/html")},
    )
    assert response.status_code == 201


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "running"}


def test_missing_prerequisite_cross_list_and_strict_status():
    load_catalog()

    student_id = "770001"
    app.state.students[student_id] = {
        "history": [
            {
                "course_code": "COSC-3506",
                "term": "23F",
                "credits_earned": 3,
                "status": "Completed",
            },
            {
                "course_code": "COSC 2006",
                "term": "23W",
                "credits_earned": 0,
                "status": "Attempted",
            },
            {
                "course_code": "COSC2006",
                "term": "24W",
                "credits_earned": 3,
                "status": "Completed",
            },
        ],
        "plan": [
            {"course_code": "ITEC 3506", "term": "26F"},
            {"course_code": "COSC 2007", "term": "24W"},
        ],
    }

    normal = client.get(f"/api/v1/students/{student_id}/audit-report")
    assert normal.status_code == 200

    body = normal.json()
    assert body["status"] == "warning"
    assert body["timeline_validation"][0]["term"] == "24W"
    assert (
        body["timeline_validation"][0]["errors"][0]["type"]
        == "MISSING_PREREQUISITE"
    )
    assert body["cross_list_violations"][0]["type"] == "CROSS_LIST_CONFLICT"
    assert body["credit_summary"] == {
        "total_earned": 6,
        "total_planned": 6,
        "total_remaining_for_graduation": 108,
    }

    strict = client.get(
        f"/api/v1/students/{student_id}/audit-report?strict=true"
    )
    assert strict.status_code == 200
    assert strict.json()["status"] == "failed"


def test_no_issues_is_ok_and_retake_not_double_counted():
    load_catalog()

    app.state.students["1"] = {
        "history": [
            {
                "course_code": "COSC 2006",
                "term": "23F",
                "credits_earned": 0,
                "status": "Attempted",
            },
            {
                "course_code": "COSC-2006",
                "term": "24W",
                "credits_earned": 3,
                "status": "Completed",
            },
            {
                "course_code": "COSC2006",
                "term": "24F",
                "credits_earned": 3,
                "status": "Completed",
            },
        ],
        "plan": [{"course_code": "COSC 2007", "term": "25W"}],
    }

    response = client.get("/api/v1/students/1/audit-report?strict=true")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["timeline_validation"] == []
    assert body["credit_summary"]["total_earned"] == 3


def test_term_order_sp_before_s_before_f():
    from main import term_key

    terms = ["26F", "26S", "26SP", "26W", "25F"]
    assert sorted(terms, key=term_key) == [
        "25F",
        "26W",
        "26SP",
        "26S",
        "26F",
    ]


def test_helpers_and_catalog_lookup():
    from main import (
        clean_text,
        display_course_code,
        extract_course_codes,
        normalize_course_code,
        parse_credits,
    )

    assert clean_text("  a   b ") == "a b"
    assert clean_text(None) == ""
    assert normalize_course_code(" cosc-3506 ") == "COSC3506"
    assert display_course_code("cosc3506") == "COSC-3506"
    assert parse_credits("3.0 credits") == 3
    assert parse_credits("none") == 0
    assert extract_course_codes("COSC 2006 or MATH-1036") == [
        "COSC2006",
        "MATH1036",
    ]
    assert extract_course_codes("None") == []

    load_catalog()

    course = client.get("/api/v1/catalog/courses/cosc-3506")
    assert course.status_code == 200
    assert course.json()["prerequisites"] == ["COSC2007"]

    missing = client.get("/api/v1/catalog/courses/NOPE1000")
    assert missing.status_code == 404


def test_catalog_rejects_bad_file_and_empty_html():
    bad_extension = client.post(
        "/api/v1/admin/catalog/import",
        files={"file": ("catalog.txt", b"hello", "text/plain")},
    )
    assert bad_extension.status_code == 400

    empty = client.post(
        "/api/v1/admin/catalog/import",
        files={"file": ("catalog.html", b"<html></html>", "text/html")},
    )
    assert empty.status_code == 400


def test_history_import_profile_plan_and_delete_routes():
    transcript = b"""
    <table>
      <tr>
        <th>Status</th>
        <th>Course</th>
        <th>Title</th>
        <th>Grade</th>
        <th>Term</th>
        <th>Credits</th>
      </tr>
      <tr>
        <td>Attempted</td>
        <td>COSC 2006</td>
        <td>PF I</td>
        <td>F</td>
        <td>23F</td>
        <td>0</td>
      </tr>
      <tr>
        <td>Completed</td>
        <td>COSC 2006</td>
        <td>PF I</td>
        <td>85</td>
        <td>24W</td>
        <td>3</td>
      </tr>
      <tr>
        <td>Completed</td>
        <td>COSC 2006</td>
        <td>PF I duplicate</td>
        <td>P</td>
        <td>24W</td>
        <td>3</td>
      </tr>
      <tr>
        <td>Unknown</td>
        <td>BAD 1000</td>
        <td>Bad</td>
        <td>A</td>
        <td>24F</td>
        <td>3</td>
      </tr>
    </table>
    """

    imported = client.post(
        "/api/v1/students/42/history/import",
        files={"file": ("student.html", transcript, "text/html")},
    )
    assert imported.status_code == 201
    assert imported.json()["past_courses_imported"] == 2

    profile = client.get("/api/v1/students/42/profile")
    assert profile.status_code == 200
    assert len(profile.json()["history"]) == 2

    plan = {
        "planned_courses": [
            {"course_code": "COSC 2007", "term": "25W"}
        ]
    }

    response = client.post("/api/v1/students/42/plan", json=plan)
    assert response.status_code == 200

    plan["planned_courses"].append(
        {"course_code": "COSC3506", "term": "25F"}
    )

    updated_plan = client.put("/api/v1/students/42/plan", json=plan)
    assert updated_plan.json()["planned_courses_saved"] == 2

    history = {
        "history": [
            {
                "course_code": "COSC 2006",
                "term": "24W",
                "credits_earned": 3,
                "status": "Completed",
            }
        ]
    }

    update_history = client.put(
        "/api/v1/students/42/history", json=history
    )
    assert update_history.status_code == 200

    delete_plan = client.delete("/api/v1/students/42/plan")
    assert delete_plan.status_code == 200

    delete_history = client.delete("/api/v1/students/42/history")
    assert delete_history.status_code == 200

    cleared = client.get("/api/v1/students/42/profile").json()
    assert cleared["history"] == []
    assert cleared["plan"] == []


def test_missing_student_routes_return_404():
    assert client.get("/api/v1/students/missing/profile").status_code == 404
    assert (
        client.get("/api/v1/students/missing/audit-report").status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/students/missing/plan",
            json={"planned_courses": []},
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/api/v1/students/missing/history",
            json={"history": []},
        ).status_code
        == 404
    )


def test_same_term_prerequisite_does_not_satisfy():
    load_catalog()

    app.state.students["same"] = {
        "history": [
            {
                "course_code": "COSC2006",
                "term": "24F",
                "credits_earned": 3,
                "status": "Completed",
            }
        ],
        "plan": [{"course_code": "COSC2007", "term": "24F"}],
    }

    response = client.get("/api/v1/students/same/audit-report")
    assert response.status_code == 200

    body = response.json()
    assert (
        body["timeline_validation"][0]["errors"][0]["message"]
        == "Missing prerequisite: COSC-2006"
    )


def test_empty_plan_credit_summary():
    app.state.students["empty"] = {"history": [], "plan": []}

    response = client.get("/api/v1/students/empty/audit-report")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert (
        body["credit_summary"]["total_remaining_for_graduation"]
        == 120
    )

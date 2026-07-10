import re
import threading
from typing import Any, Dict, List, Tuple

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

app = FastAPI(title="Student Academic Profile API")

app.state.catalog = {}
app.state.students = {}
app.state.lock = threading.RLock()


class HistoryCourse(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str


class HistoryPayload(BaseModel):
    history: List[HistoryCourse]


class PlannedCourse(BaseModel):
    course_code: str
    term: str


class PlanPayload(BaseModel):
    planned_courses: List[PlannedCourse]


def to_dict(model: BaseModel) -> Dict[str, Any]:
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def normalize_course_code(value: str) -> str:
    """Make COSC 3506, COSC-3506 and cosc3506 compare equally."""
    return re.sub(r"[\s-]+", "", clean_text(value)).upper()


def display_course_code(value: str) -> str:
    """Return a stable, readable course code such as COSC-3506."""
    normalized = normalize_course_code(value)
    match = re.fullmatch(r"([A-Z]+)(\d+[A-Z]?)", normalized)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return clean_text(value).upper().replace(" ", "-")


def parse_credits(value: Any) -> int:
    match = re.search(r"\d+(?:\.\d+)?", clean_text(value))
    return int(float(match.group())) if match else 0


def extract_course_codes(value: str) -> List[str]:
    text = clean_text(value)
    if not text or text.lower() in {"none", "n/a", "na", "-"}:
        return []

    # Catalogs commonly use commas, slashes, semicolons, "or", and prose.
    found = re.findall(r"\b[A-Za-z]{2,}\s*-?\s*\d{3,4}[A-Za-z]?\b", text)
    result: List[str] = []
    seen = set()
    for code in found:
        normalized = normalize_course_code(code)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def term_key(term: str) -> Tuple[int, int]:
    """Canonical order: W < SP < S < F for two-digit-year terms."""
    normalized = re.sub(r"[\s-]+", "", clean_text(term)).upper()
    match = re.fullmatch(r"(\d{2})(W|SP|S|F)", normalized)
    if not match:
        # Keep malformed terms deterministic and after normal canonical terms.
        return (999, 999)
    season_order = {"W": 0, "SP": 1, "S": 2, "F": 3}
    return (int(match.group(1)), season_order[match.group(2)])


def grade_rank(grade: str) -> int:
    grade = clean_text(grade).upper()
    if not grade:
        return 0
    if re.search(r"\d", grade):
        return 3
    if re.fullmatch(r"[A-F][+-]?", grade):
        return 2
    if grade in {"P", "PASS"}:
        return 1
    return 1


def find_transcript_header(cells: List[str]) -> Dict[str, int]:
    aliases = {
        "status": {"status"},
        "course": {"course", "course code", "course_code"},
        "grade": {"grade"},
        "term": {"term"},
        "credits": {"credits", "credit", "credits earned", "credits_earned"},
    }
    normalized = [clean_text(cell).lower() for cell in cells]
    result: Dict[str, int] = {}
    for index, header in enumerate(normalized):
        for key, names in aliases.items():
            if header in names:
                result[key] = index
    return result if set(aliases).issubset(result) else {}


def parse_transcript_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    valid_statuses = {"completed", "in-progress", "in progress", "attempted"}
    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        header_map: Dict[str, int] = {}
        header_index = -1
        for index, row in enumerate(rows):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            header_map = find_transcript_header(cells)
            if header_map:
                header_index = index
                break
        if not header_map:
            continue

        for row in rows[header_index + 1 :]:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) <= max(header_map.values()):
                continue

            raw_status = clean_text(cells[header_map["status"]])
            status_key = raw_status.lower()
            if status_key not in valid_statuses:
                continue
            canonical_status = (
                "Completed"
                if status_key == "completed"
                else (
                    "In-Progress" if status_key in {"in-progress", "in progress"} else "Attempted"
                )
            )

            raw_code = clean_text(cells[header_map["course"]])
            term = clean_text(cells[header_map["term"]]).upper()
            if not raw_code or not term:
                continue

            normalized_code = normalize_course_code(raw_code)
            candidate = {
                "course_code": display_course_code(raw_code),
                "term": term,
                "credits_earned": parse_credits(cells[header_map["credits"]]),
                "status": canonical_status,
                "_grade_rank": grade_rank(cells[header_map["grade"]]),
            }
            key = (normalized_code, term)
            existing = deduped.get(key)
            if existing is None or (candidate["_grade_rank"], candidate["credits_earned"]) > (
                existing["_grade_rank"],
                existing["credits_earned"],
            ):
                deduped[key] = candidate

    history = []
    for item in deduped.values():
        item.pop("_grade_rank", None)
        history.append(item)
    history.sort(
        key=lambda item: (term_key(item["term"]), normalize_course_code(item["course_code"]))
    )
    return history


def find_catalog_header(cells: List[str]) -> Dict[str, int]:
    aliases = {
        "course_code": {"course code", "course", "code", "course_code"},
        "title": {"title", "course title", "name"},
        "credits": {"credits", "credit"},
        "prerequisites": {"prerequisites", "prerequisite", "pre-requisites", "pre requisite"},
        "cross_listed": {
            "cross-listed",
            "cross listed",
            "cross-listing",
            "cross listing",
            "cross_listed",
        },
    }
    normalized = [clean_text(cell).lower() for cell in cells]
    result: Dict[str, int] = {}
    for index, header in enumerate(normalized):
        for key, names in aliases.items():
            if header in names:
                result[key] = index
    required = {"course_code", "credits"}
    return result if required.issubset(result) else {}


def parse_catalog_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    courses: Dict[str, Dict[str, Any]] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        header_map: Dict[str, int] = {}
        header_index = -1
        for index, row in enumerate(rows):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            header_map = find_catalog_header(cells)
            if header_map:
                header_index = index
                break
        if not header_map:
            continue

        for row in rows[header_index + 1 :]:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) <= max(header_map.values()):
                continue
            raw_code = clean_text(cells[header_map["course_code"]])
            code = normalize_course_code(raw_code)
            if not code:
                continue
            title = clean_text(cells[header_map["title"]]) if "title" in header_map else ""
            prereq_text = (
                cells[header_map["prerequisites"]] if "prerequisites" in header_map else ""
            )
            cross_text = cells[header_map["cross_listed"]] if "cross_listed" in header_map else ""
            courses[code] = {
                "course_code": display_course_code(raw_code),
                "title": title,
                "credits": parse_credits(cells[header_map["credits"]]),
                "prerequisites": extract_course_codes(prereq_text),
                "cross_listed": extract_course_codes(cross_text),
            }

    return list(courses.values())


def get_student_or_404(student_id: str) -> Dict[str, Any]:
    student = app.state.students.get(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    return student


def completed_credit_map(history: List[Dict[str, Any]]) -> Dict[str, int]:
    """One completed credit value per normalized course; failures never add credit."""
    completed: Dict[str, int] = {}
    for entry in history:
        if clean_text(entry.get("status")).lower() != "completed":
            continue
        code = normalize_course_code(entry.get("course_code", ""))
        if not code:
            continue
        completed[code] = max(completed.get(code, 0), int(entry.get("credits_earned", 0) or 0))
    return completed


def build_audit_report(student_id: str, strict: bool) -> Dict[str, Any]:
    student = get_student_or_404(student_id)
    history = list(student.get("history", []))
    plan = list(student.get("plan", []))
    catalog = app.state.catalog

    completed_entries = [
        entry for entry in history if clean_text(entry.get("status")).lower() == "completed"
    ]
    completed_codes = {
        normalize_course_code(entry.get("course_code", "")) for entry in completed_entries
    }

    errors_by_term: Dict[str, List[Dict[str, str]]] = {}
    cross_list_violations: List[Dict[str, str]] = []
    seen_cross_conflicts = set()

    for planned in sorted(
        plan,
        key=lambda item: (
            term_key(item.get("term", "")),
            normalize_course_code(item.get("course_code", "")),
        ),
    ):
        raw_planned_code = planned.get("course_code", "")
        planned_code = normalize_course_code(raw_planned_code)
        planned_display = display_course_code(raw_planned_code)
        planned_term = clean_text(planned.get("term", "")).upper()
        course = catalog.get(planned_code, {})

        for prereq_code in course.get("prerequisites", []):
            satisfied = any(
                normalize_course_code(entry.get("course_code", "")) == prereq_code
                and term_key(entry.get("term", "")) < term_key(planned_term)
                for entry in completed_entries
            )
            if not satisfied:
                prereq_display = catalog.get(prereq_code, {}).get(
                    "course_code", display_course_code(prereq_code)
                )
                errors_by_term.setdefault(planned_term, []).append(
                    {
                        "course_code": planned_display,
                        "type": "MISSING_PREREQUISITE",
                        "message": f"Missing prerequisite: {prereq_display}",
                    }
                )

        for cross_code in course.get("cross_listed", []):
            if cross_code in completed_codes:
                conflict_key = (planned_code, cross_code)
                if conflict_key in seen_cross_conflicts:
                    continue
                seen_cross_conflicts.add(conflict_key)
                completed_display = catalog.get(cross_code, {}).get(
                    "course_code", display_course_code(cross_code)
                )
                cross_list_violations.append(
                    {
                        "course_code": planned_display,
                        "type": "CROSS_LIST_CONFLICT",
                        "message": f"Cross-listed with completed course {completed_display}",
                    }
                )

    timeline_validation = [
        {"term": term, "errors": errors_by_term[term]}
        for term in sorted(errors_by_term, key=term_key)
    ]

    total_earned = sum(completed_credit_map(history).values())
    total_planned = sum(
        int(
            catalog.get(normalize_course_code(item.get("course_code", "")), {}).get("credits", 0)
            or 0
        )
        for item in plan
    )
    total_remaining = max(0, 120 - total_earned - total_planned)

    has_issues = bool(timeline_validation or cross_list_violations)
    report_status = "failed" if has_issues and strict else ("warning" if has_issues else "ok")

    return {
        "student_id": student_id,
        "status": report_status,
        "timeline_validation": timeline_validation,
        "cross_list_violations": cross_list_violations,
        "credit_summary": {
            "total_earned": total_earned,
            "total_planned": total_planned,
            "total_remaining_for_graduation": total_remaining,
        },
    }


@app.get("/")
def root() -> Dict[str, str]:
    return {"status": "running"}


@app.post("/api/v1/admin/catalog/import", status_code=status.HTTP_201_CREATED)
async def import_catalog(file: UploadFile = File(...)) -> Dict[str, Any]:
    if file.filename and not file.filename.lower().endswith((".html", ".htm")):
        raise HTTPException(status_code=400, detail="Only HTML files are allowed")
    html = (await file.read()).decode("utf-8", errors="ignore")
    parsed = parse_catalog_html(html)
    if not parsed:
        raise HTTPException(status_code=400, detail="No catalog courses found")
    with app.state.lock:
        app.state.catalog = {
            normalize_course_code(course["course_code"]): course for course in parsed
        }
    return {"status": "success", "courses_imported": len(parsed)}


@app.get("/api/v1/catalog/courses/{course_code}")
def get_catalog_course(course_code: str) -> Dict[str, Any]:
    course = app.state.catalog.get(normalize_course_code(course_code))
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
    return course


@app.post("/api/v1/students/{student_id}/history/import", status_code=status.HTTP_201_CREATED)
async def import_history(student_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    html = (await file.read()).decode("utf-8", errors="ignore")
    parsed_history = parse_transcript_html(html)
    with app.state.lock:
        student = app.state.students.setdefault(student_id, {"history": [], "plan": []})
        student["history"] = parsed_history
    return {"status": "success", "past_courses_imported": len(parsed_history)}


@app.put("/api/v1/students/{student_id}/history")
def update_history(student_id: str, payload: HistoryPayload) -> Dict[str, str]:
    with app.state.lock:
        student = get_student_or_404(student_id)
        student["history"] = [to_dict(course) for course in payload.history]
    return {"status": "success", "message": "Academic history updated successfully"}


@app.delete("/api/v1/students/{student_id}/history")
def delete_history(student_id: str) -> Dict[str, str]:
    with app.state.lock:
        get_student_or_404(student_id)["history"] = []
    return {"status": "success", "message": "Academic history cleared successfully"}


@app.post("/api/v1/students/{student_id}/plan")
def create_plan(student_id: str, payload: PlanPayload) -> Dict[str, Any]:
    with app.state.lock:
        student = get_student_or_404(student_id)
        student["plan"] = [to_dict(course) for course in payload.planned_courses]
    return {"status": "success", "planned_courses_saved": len(payload.planned_courses)}


@app.put("/api/v1/students/{student_id}/plan")
def update_plan(student_id: str, payload: PlanPayload) -> Dict[str, Any]:
    return create_plan(student_id, payload)


@app.delete("/api/v1/students/{student_id}/plan")
def delete_plan(student_id: str) -> Dict[str, str]:
    with app.state.lock:
        get_student_or_404(student_id)["plan"] = []
    return {"status": "success", "message": "Plan cleared successfully"}


@app.get("/api/v1/students/{student_id}/profile")
def get_profile(student_id: str) -> Dict[str, Any]:
    student = get_student_or_404(student_id)
    return {"student_id": student_id, "history": student["history"], "plan": student["plan"]}


@app.get("/api/v1/students/{student_id}/audit-report")
def get_audit_report(
    student_id: str,
    strict: bool = Query(default=False),
) -> Dict[str, Any]:
    with app.state.lock:
        return build_audit_report(student_id, strict)

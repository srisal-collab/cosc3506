from fastapi import FastAPI, UploadFile, File, HTTPException, status
from pydantic import BaseModel
from bs4 import BeautifulSoup
from typing import List, Dict, Any
import re
import threading


app = FastAPI(title="Student Academic Profile API")


app.state.students = {}
app.state.lock = threading.Lock()




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



def to_dict(model):
    """
    Supports both Pydantic v1 and v2.
    """
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def clean_text(text: str) -> str:
    if text is None:
        return ""
    return " ".join(str(text).strip().split())


def parse_credits(value: str) -> int:
    """
    Credits cell must become integer.
    Blank or non-numeric values become 0.
    """
    value = clean_text(value)

    if not value:
        return 0

    match = re.search(r"\d+(\.\d+)?", value)

    if not match:
        return 0

    return int(float(match.group()))


def grade_rank(grade: str) -> int:
    """
    Deduplication rule:
    numeric grade beats letter grade beats P/blank.
    """
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


def find_header_map(cells: List[str]) -> Dict[str, int]:
    """
    Transcript table header contains:
    Status, Course, Grade, Term, Credits

    There may be a title column too, which we ignore.
    """
    normalized = [clean_text(cell).lower() for cell in cells]

    header_map = {}

    for index, header in enumerate(normalized):
        if header == "status":
            header_map["status"] = index
        elif header == "course":
            header_map["course"] = index
        elif header == "grade":
            header_map["grade"] = index
        elif header == "term":
            header_map["term"] = index
        elif header == "credits":
            header_map["credits"] = index

    required = {"status", "course", "grade", "term", "credits"}

    if required.issubset(header_map.keys()):
        return header_map

    return {}


def parse_transcript_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")

    valid_statuses = {"Completed", "In-Progress", "Attempted"}

   
    deduped = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        header_map = None
        header_row_index = None


        for i, row in enumerate(rows):       
            cells = [    
                cell.get_text(" ", strip=True)
                for cell in row.find_all(["th", "td"])
            ]

            possible_header = find_header_map(cells)

            if possible_header:
                header_map = possible_header
                header_row_index = i
                break

        if not header_map:
            continue

        
        for row in rows[header_row_index + 1:]: 
            cells = [
                cell.get_text(" ", strip=True)
                for cell in row.find_all(["td", "th"])
            ]

            if not cells:
                continue

            max_index = max(header_map.values())

            if len(cells) <= max_index:
                continue

            status_value = clean_text(cells[header_map["status"]]) 
            course_code = clean_text(cells[header_map["course"]]) 
            grade = clean_text(cells[header_map["grade"]])
            term = clean_text(cells[header_map["term"]])
            credits_earned = parse_credits(cells[header_map["credits"]])

    
            if status_value not in valid_statuses:
                continue 

            if not term:
                continue

            if not course_code:
                continue

            key = (course_code, term)

            candidate = {
                "course_code": course_code,
                "term": term,
                "credits_earned": credits_earned,
                "status": status_value,
                "_grade_rank": grade_rank(grade),
            }

            if key not in deduped:
                deduped[key] = candidate
            else:
                existing = deduped[key]

               
                if candidate["_grade_rank"] > existing["_grade_rank"]:  
                    deduped[key] = candidate

               
                elif candidate["_grade_rank"] == existing["_grade_rank"]:  
                    if candidate["credits_earned"] > existing["credits_earned"]:
                        deduped[key] = candidate

    history = []

    for item in deduped.values():
        item.pop("_grade_rank", None)
        history.append(item)

    return history


def get_student_or_404(student_id: str) -> Dict[str, Any]:
    student = app.state.students.get(student_id)

    if student is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found"
        )

    return student



@app.get("/")
def root():
    return {"status": "running"}



@app.post(
    "/api/v1/students/{student_id}/history/import",
    status_code=status.HTTP_201_CREATED
)
async def import_history(student_id: str, file: UploadFile = File(...)):
    content = await file.read()
    html = content.decode("utf-8", errors="ignore")

    parsed_history = parse_transcript_html(html)

    with app.state.lock:
        if student_id not in app.state.students:
            app.state.students[student_id] = {
                "history": [],
                "plan": []
            }

        app.state.students[student_id]["history"] = parsed_history

    return {
        "status": "success",
        "past_courses_imported": len(parsed_history)
    }


@app.put("/api/v1/students/{student_id}/history")
def update_history(student_id: str, payload: HistoryPayload):
    with app.state.lock:
        student = get_student_or_404(student_id)
        student["history"] = [to_dict(course) for course in payload.history]

    return {
        "status": "success",
        "message": "Academic history updated successfully"
    }


@app.delete("/api/v1/students/{student_id}/history")
def delete_history(student_id: str):
    with app.state.lock:
        student = get_student_or_404(student_id)
        student["history"] = []

    return {
        "status": "success",
        "message": "Academic history cleared successfully"
    }




@app.post("/api/v1/students/{student_id}/plan")
def create_plan(student_id: str, payload: PlanPayload):
    with app.state.lock:
        student = get_student_or_404(student_id)
        student["plan"] = [to_dict(course) for course in payload.planned_courses]

    return {
        "status": "success",
        "planned_courses_saved": len(payload.planned_courses)
    }


@app.put("/api/v1/students/{student_id}/plan")
def update_plan(student_id: str, payload: PlanPayload):
    with app.state.lock:
        student = get_student_or_404(student_id)
        student["plan"] = [to_dict(course) for course in payload.planned_courses]

    return {
        "status": "success",
        "planned_courses_saved": len(payload.planned_courses)
    }


@app.delete("/api/v1/students/{student_id}/plan")
def delete_plan(student_id: str):
    with app.state.lock:
        student = get_student_or_404(student_id)
        student["plan"] = []

    return {
        "status": "success",
        "message": "Plan cleared successfully"
    }



@app.get("/api/v1/students/{student_id}/profile")
def get_profile(student_id: str):
    student = get_student_or_404(student_id)

    return {
        "student_id": student_id,
        "history": student["history"],
        "plan": student["plan"]
    }
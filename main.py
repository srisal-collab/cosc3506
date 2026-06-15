from fastapi import FastAPI, UploadFile, File, HTTPException
from bs4 import BeautifulSoup
import re

app = FastAPI()

courses = {}


def clean_course_code(code: str) -> str:
    return code.replace(" ", "").strip().upper()


def extract_course_codes(text: str):
    if not text or text.strip().lower() == "none":
        return []

    pattern = r"[A-Z]{4}\s?\d{4}"
    found = re.findall(pattern, text.upper())

    return [clean_course_code(code) for code in found]


@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    if not file.filename.endswith(".html"):
        raise HTTPException(status_code=400, detail="Only HTML files are allowed")

    content = await file.read()
    soup = BeautifulSoup(content, "html.parser")

    table = soup.find("table")
    if table is None:
        raise HTTPException(status_code=400, detail="No table found in HTML file")

    rows = table.find("tbody").find_all("tr")

    imported_count = 0

    for row in rows:
        columns = row.find_all("td")

        if len(columns) < 5:
            continue

        course_code = clean_course_code(columns[0].get_text(strip=True))
        title = columns[1].get_text(strip=True)
        credits = int(columns[2].get_text(strip=True))
        prerequisites_text = columns[3].get_text(strip=True)
        cross_listed_text = columns[4].get_text(strip=True)

        courses[course_code] = {
            "course_code": course_code,
            "title": title,
            "credits": credits,
            "prerequisites": extract_course_codes(prerequisites_text),
            "cross_listed": extract_course_codes(cross_listed_text)
        }

        imported_count += 1

    return {
        "message": "Catalog imported successfully",
        "imported_courses": imported_count
    }


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    course_code = clean_course_code(course_code)

    if course_code not in courses:
        raise HTTPException(status_code=404, detail="Course not found")

    return courses[course_code]
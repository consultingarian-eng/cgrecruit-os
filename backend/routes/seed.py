"""Demo seed endpoint — populates pipelines/jobs/candidates for first-time recruiters."""
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends

from deps import db, current_user, require_super_admin
from models import Pipeline, Job, Candidate

router = APIRouter()


@router.post("/seed/demo")
async def seed_demo(user: dict = Depends(current_user)):
    """Seed demo pipelines, jobs, and candidates so the dashboard isn't empty.
    Owner only: it writes offices into the account."""
    require_super_admin(user)
    user_id = user["id"]
    pcount = await db.pipelines.count_documents({"user_id": user_id})
    if pcount > 0:
        return {"ok": True, "skipped": True}

    pipelines = []
    # Fictional demo offices — they match the example offices in
    # backend/company_profile.json, so office-aware features light up.
    pipeline_specs = [
        {
            "name": "Downtown",
            "desc": "Downtown office (demo)",
            "twilio": "",
            "additional_context_override": (
                "This applicant is for the DOWNTOWN office.\n"
                "- Office location: city centre, close to the main train and bus stations.\n"
                "- Most suitable for candidates who can commute into the city centre.\n"
                "- Local team led by the Downtown hiring manager."
            ),
        },
        {
            "name": "Riverside",
            "desc": "Riverside office (demo)",
            "twilio": "",
            "additional_context_override": (
                "This applicant is for the RIVERSIDE office.\n"
                "- Office location: Riverside business park, free parking on site.\n"
                "- Local team led by the Riverside hiring manager."
            ),
        },
        {
            "name": "Military Hire",
            "desc": "Veterans hiring program (demo)",
            "twilio": "",
            "additional_context_override": (
                "This applicant is part of our MILITARY HIRE program — veterans, transitioning service members, and military spouses.\n"
                "- Be especially appreciative of their service when introducing yourself.\n"
                "- We accept candidates for either office; ask which is most commutable."
            ),
        },
    ]
    for spec in pipeline_specs:
        slug = spec["name"].lower().replace(",", "").replace(" ", "-")
        p = Pipeline(
            user_id=user_id,
            name=spec["name"],
            description=spec["desc"],
            twilio_phone_number=spec["twilio"],
            public_slug=slug,
            additional_context_override=spec["additional_context_override"],
        )
        await db.pipelines.insert_one(p.model_dump())
        pipelines.append(p)

    jobs = []
    for p in pipelines:
        j = Job(
            user_id=user_id, pipeline_id=p.id,
            title="Customer Service / Sales Representative",
            category="Customer Services",
            description=(
                "We are seeking a results-driven Customer Service / Sales Representative to deliver exceptional client experiences and grow accounts.\n\n"
                "Hours: EDIT ME — e.g. 10:00 AM – 6:00 PM, Monday–Friday.\n"
                "Compensation: EDIT ME — describe base pay, commission and bonuses.\n"
                "Location: in-person at our office. Interviews held by video call (30 min)."
            ),
            city=p.name.split(",")[0].strip() if "," in p.name else p.name,
            country="", is_active=True,
        )
        await db.jobs.insert_one(j.model_dump())
        jobs.append(j)

    # Fictional people with reserved 555-01xx numbers and example.com emails.
    sample_candidates = [
        ("Rowan", "Sample", "rowan.sample@example.com", "+15550100101", "APPLICANT", "pending", 78),
        ("Kai", "Example", "kai.example@example.com", "+15550100102", "SCREENING", "in_progress", 84),
        ("Emery", "Testwood", "emery.testwood@example.com", "+15550100103", "SCREENING", "approved", 91),
        ("Sage", "Mockley", "sage.mockley@example.com", "+15550100104", "APPOINTMENT", "approved", 72),
        ("Toby", "Northwind", "toby.northwind@example.com", "+15550100105", "APPOINTMENT", "approved", 88),
        ("Indigo", "Varnley", "indigo.varnley@example.com", "+15550100106", "FORM", "approved", 76),
        ("Marlo", "Brightwell", "marlo.brightwell@example.com", "+15550100107", "CLOSE", "approved", 80),
        ("Galen", "Ashgrove", "galen.ashgrove@example.com", "+15550100108", "TRAINING", "approved", 85),
        ("Juno", "Fernhollow", "juno.fernhollow@example.com", "+15550100109", "SCREENING", "no_answer", 65),
        ("Nico", "Pemberly", "nico.pemberly@example.com", "+15550100110", "APPLICANT", "pending", 70),
        ("Tamsin", "Oakhurst", "tamsin.oakhurst@example.com", "+15550100111", "SCREENING", "didnt_connect", 55),
        ("Arlo", "Larkspur", "arlo.larkspur@example.com", "+15550100112", "APPOINTMENT", "approved", 90),
    ]
    appt_base = datetime.now(timezone.utc) + timedelta(days=2)
    for i, (fn, ln, em, ph, stage, status, score) in enumerate(sample_candidates):
        p = pipelines[i % len(pipelines)]
        j = jobs[i % len(jobs)]
        appointment_at = None
        appointment_recruiter = None
        appointment_link = None
        if stage in ("APPOINTMENT", "FORM", "CLOSE", "TRAINING"):
            appointment_at = (appt_base + timedelta(hours=i * 3)).isoformat()
            appointment_recruiter = "Demo Hiring Manager"
            appointment_link = "https://meet.example.com/demo-interview"
        c = Candidate(
            user_id=user_id, pipeline_id=p.id, job_id=j.id,
            first_name=fn, last_name=ln, email=em, phone=ph,
            stage=stage, screening_status=status,
            smart_score=score,
            smart_score_rationale=f"Strong alignment on {j.title.lower()} responsibilities; {('above-average' if score >= 80 else 'reasonable')} match on experience.",
            appointment_at=appointment_at,
            appointment_recruiter=appointment_recruiter,
            appointment_link=appointment_link,
            parsed_resume={
                "first_name": fn, "last_name": ln, "email": em, "phone": ph,
                "summary": f"{fn} brings 5+ years of customer-facing experience with a proven track record of exceeding sales targets.",
                "skills": ["Customer Service", "Sales", "CRM", "Negotiation", "Communication"],
                "experience": [{"company": "Acme Co", "title": "Senior Sales Rep", "start": "2021", "end": "Present", "description": "Led territory growth +28% YoY."}],
                "education": [{"institution": "State University", "degree": "B.A. Communications", "year": "2020"}],
                "years_experience": 5,
            },
            rating=3 if score >= 80 else 2,
        )
        await db.candidates.insert_one(c.model_dump())

    return {"ok": True, "pipelines": len(pipelines), "candidates": len(sample_candidates)}

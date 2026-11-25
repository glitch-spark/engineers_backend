"""URL cleaning, ported from job_url_filter (tests/test_normalize.py) with its case table. Differences: the
scheme is kept, and only the scheme's default port is dropped."""
import pytest

from app.job_apply.urls import normalize_all, normalize_url
from app.models.resume_application import job_url_key


@pytest.mark.parametrize(
    "original, expected",
    [
        # --- Query strings from the request ---------------------------------
        (
            "https://app.careerpuck.com/job-board/prenuvo/job/4739947005?gh_jid=4739947005",
            "https://app.careerpuck.com/job-board/prenuvo/job/4739947005",
        ),
        (
            "https://numentica.zohorecruit.com/jobs/careers/253104000022182177/QA-Engineer?source=CareerSite",
            "https://numentica.zohorecruit.com/jobs/careers/253104000022182177/QA-Engineer",
        ),
        (
            "https://careers-peraton.icims.com/jobs/171576/senior-full-stack-engineer/job?mobile=false&width=1896"
            "&height=500&bga=true&needsRedirect=false&jan1offset=-480&jun1offset=-420",
            "https://careers-peraton.icims.com/jobs/171576/senior-full-stack-engineer/job",
        ),
        (
            "https://flexerasoftware.wd1.myworkdayjobs.com/flexerasoftware/job/Home-United-States/"
            "Senior-Software-Engineer_17541-2?jr_id=6abe90e44ac55253f5d615c7",
            "https://flexerasoftware.wd1.myworkdayjobs.com/flexerasoftware/job/Home-United-States/"
            "Senior-Software-Engineer_17541-2",
        ),
        (
            "https://join.com/companies/gertrud/16536219-senior-full-stack-engineer-f-asp-net-core-reactjs-remote-europe-f-m-d"
            "?pid=0a9968ed210507e82356",
            "https://join.com/companies/gertrud/16536219-senior-full-stack-engineer-f-asp-net-core-reactjs-remote-europe-f-m-d",
        ),
        # JOIN company pages (not a job) keep their query.
        ("https://join.com/companies/gertrud?pid=abc", "https://join.com/companies/gertrud?pid=abc"),
        # --- Fragments ------------------------------------------------------
        (
            "https://jobs.lever.co/acme/1a2b3c4d-0000-1111-2222-333344445555#apply",
            "https://jobs.lever.co/acme/1a2b3c4d-0000-1111-2222-333344445555",
        ),
        ("https://careers.acme.com/#/job/123", "https://careers.acme.com/#/job/123"),
        ("https://careers.acme.com/#!/job/123", "https://careers.acme.com/#!/job/123"),
        # --- Apply pages -> job description ---------------------------------
        (
            "https://jobs.lever.co/acme/1a2b3c4d-0000-1111-2222-333344445555/apply",
            "https://jobs.lever.co/acme/1a2b3c4d-0000-1111-2222-333344445555",
        ),
        (
            "https://jobs.ashbyhq.com/acme/9f3c0000-aaaa-bbbb-cccc-ddddeeeeffff/application",
            "https://jobs.ashbyhq.com/acme/9f3c0000-aaaa-bbbb-cccc-ddddeeeeffff",
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/Careers/job/New-York/Engineer_R123/apply",
            "https://acme.wd5.myworkdayjobs.com/Careers/job/New-York/Engineer_R123",
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/Careers/job/New-York/Engineer_R123/apply/applyManually",
            "https://acme.wd5.myworkdayjobs.com/Careers/job/New-York/Engineer_R123",
        ),
        ("https://apply.workable.com/acme/j/AB12CD34EF/apply/", "https://apply.workable.com/acme/j/AB12CD34EF"),
        ("https://acme.com/jobs/123/apply-now", "https://acme.com/jobs/123"),
        (
            "https://careers-acme.icims.com/jobs/171576/senior-engineer/login",
            "https://careers-acme.icims.com/jobs/171576/senior-engineer/job",
        ),
        (
            "https://www.indeed.com/applystart?jk=abc123def&from=serp",
            "https://www.indeed.com/viewjob?jk=abc123def",
        ),
        ("https://acme.com/jobs?id=5&apply=true", "https://acme.com/jobs?id=5"),
        # Too short to know the page left behind is a job: keep.
        ("https://acme.com/careers/apply", "https://acme.com/careers/apply"),
        ("https://acme.com/apply", "https://acme.com/apply"),
        # --- Workday language segment ---------------------------------------
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/New-York/Engineer_R123?source=LinkedIn",
            "https://acme.wd5.myworkdayjobs.com/Careers/job/New-York/Engineer_R123",
        ),
        # --- LinkedIn -------------------------------------------------------
        (
            "https://uk.linkedin.com/jobs/view/senior-engineer-at-acme-3812345678/?refId=x&trackingId=y",
            "https://www.linkedin.com/jobs/view/3812345678",
        ),
        ("https://www.linkedin.com/comm/jobs/view/3812345678", "https://www.linkedin.com/jobs/view/3812345678"),
        (
            "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=3812345678&origin=x",
            "https://www.linkedin.com/jobs/view/3812345678",
        ),
        ("https://www.linkedin.com/jobs/view/3812345678", "https://www.linkedin.com/jobs/view/3812345678"),
        # --- Indeed ---------------------------------------------------------
        (
            "https://uk.indeed.com/viewjob?jk=abc123def&from=serp&tk=1h",
            "https://uk.indeed.com/viewjob?jk=abc123def",
        ),
        ("https://www.indeed.com/jobs?q=python&vjk=abc123def", "https://www.indeed.com/viewjob?jk=abc123def"),
        ("https://www.indeed.com/rc/clk?jk=abc123def&bb=zz", "https://www.indeed.com/viewjob?jk=abc123def"),
        # --- Greenhouse -----------------------------------------------------
        (
            "https://boards.greenhouse.io/embed/job_app?for=acme&token=4739947005&gh_src=x",
            "https://job-boards.greenhouse.io/acme/jobs/4739947005",
        ),
        (
            "https://job-boards.greenhouse.io/acme/jobs/4739947005?gh_src=abc",
            "https://job-boards.greenhouse.io/acme/jobs/4739947005",
        ),
        (
            "https://boards.greenhouse.io/embed/job_board?for=acme",
            "https://boards.greenhouse.io/embed/job_board?for=acme",
        ),
        # --- Session ids, slashes, scheme, case, port -----------------------
        (
            "https://acme.taleo.net/careersection/2/jobdetail.ftl;jsessionid=A1B2C3?job=123",
            "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=123",
        ),
        ("https://jobs.ashbyhq.com/acme/9f3c/", "https://jobs.ashbyhq.com/acme/9f3c"),
        ("https://acme.com/careers//jobs/123", "https://acme.com/careers/jobs/123"),
        ("HTTP://Jobs.Lever.CO:80/acme/1a2b", "http://jobs.lever.co/acme/1a2b"),
        ("https://jobs.lever.co:443/acme/1a2b", "https://jobs.lever.co/acme/1a2b"),
        ("http://jobs.lever.co:443/acme/1a2b", "http://jobs.lever.co:443/acme/1a2b"),
        ("http://acme.com:8080/jobs/1", "http://acme.com:8080/jobs/1"),
        ("www.acme.com/jobs/1", "https://www.acme.com/jobs/1"),
        # --- ID already in the path / ID only in the query ------------------
        (
            "https://careers.acme.com/job/4739947005?gh_jid=4739947005",
            "https://careers.acme.com/job/4739947005",
        ),
        ("https://acme.com/careers?gh_jid=4739947005", "https://acme.com/careers?gh_jid=4739947005"),
        # --- Tracking removed on unknown sites, everything else kept --------
        ("https://acme.com/jobs?id=5&utm_source=x&UTM_Medium=y&gclid=z", "https://acme.com/jobs?id=5"),
        ("https://acme.com/jobs/view?id=55&lang=en", "https://acme.com/jobs/view?id=55&lang=en"),
        ("https://acme.com/jobs?q=a%20b+c&x=%2F", "https://acme.com/jobs?q=a%20b+c&x=%2F"),
        # --- Query strings that ARE the job id: never removed ---------------
        (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=abc&jobId=123",
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=abc&jobId=123",
        ),
        (
            "https://career4.successfactors.com/career?company=acme&career_job_req_id=123",
            "https://career4.successfactors.com/career?company=acme&career_job_req_id=123",
        ),
        (
            "https://recruiting.ultipro.com/ACM1000/JobBoard/abc/OpportunityDetail?opportunityId=9f3c",
            "https://recruiting.ultipro.com/ACM1000/JobBoard/abc/OpportunityDetail?opportunityId=9f3c",
        ),
        # --- Already clean / unparseable ------------------------------------
        (
            "https://job-boards.greenhouse.io/acme/jobs/4739947005",
            "https://job-boards.greenhouse.io/acme/jobs/4739947005",
        ),
        ("https://[not-a-host/jobs", "https://[not-a-host/jobs"),
    ],
)
def test_normalize_url(original, expected):
    assert normalize_url(original) == expected


def test_normalize_is_idempotent():
    url = "https://uk.linkedin.com/jobs/view/senior-engineer-at-acme-3812345678/?refId=x"
    once = normalize_url(url)
    assert normalize_url(once) == once


def test_normalize_all_counts_changes_and_keeps_order():
    urls = [
        "https://jobs.lever.co/acme/1a2b/apply",
        "https://job-boards.greenhouse.io/acme/jobs/1",
        "https://jobs.lever.co/acme/1a2b/apply",
    ]
    normalized, changed = normalize_all(urls)
    assert normalized == [
        "https://jobs.lever.co/acme/1a2b",
        "https://job-boards.greenhouse.io/acme/jobs/1",
        "https://jobs.lever.co/acme/1a2b",
    ]
    assert changed == 2


def test_normalize_all_empty():
    assert normalize_all([]) == ([], 0)


# Bid Track records the page the bidder submitted from (an apply step); the resume holds the posting link.
# Each pair is one job seen from both sides in production data; they must clean to the same job page.
@pytest.mark.parametrize(
    "bid_url, resume_url",
    [
        ("https://akka.pinpointhq.com/en/postings/29008c2b-c99d-40dc-a59e-eaa9838dc945/applications/new",
         "https://akka.pinpointhq.com/en/postings/29008c2b-c99d-40dc-a59e-eaa9838dc945"),
        ("https://receptive.pinpointhq.com/en/postings/ffa7e28f-e861-4cf0-8bbd-153fc1f6e499/applications/thanks?token=z9Q",
         "https://receptive.pinpointhq.com/en/postings/ffa7e28f-e861-4cf0-8bbd-153fc1f6e499"),
        ("https://cplugjobs.careerplug.com/jobs/3611892/apps/new", "https://cplugjobs.careerplug.com/jobs/3611892"),
        ("https://planatechnologies.peopleforce.io/careers/v/252369-fullstack-engineer-applied-ai/a/new",
         "https://planatechnologies.peopleforce.io/careers/v/252369-fullstack-engineer-applied-ai"),
        ("https://jobs.lever.co/magnetforensics/efd7e6e1-2cde-4010-8b75-b9500cfdf748/thanks",
         "https://jobs.lever.co/magnetforensics/efd7e6e1-2cde-4010-8b75-b9500cfdf748"),
        ("https://job-boards.greenhouse.io/keepersecurity/jobs/4438886009/confirmation",
         "https://job-boards.greenhouse.io/keepersecurity/jobs/4438886009"),
        ("https://app.loxo.co/job/MzY5MzEtdnpwMDFka25ka2dydmFlOQ==/form?source_type=app",
         "https://app.loxo.co/job/MzY5MzEtdnpwMDFka25ka2dydmFlOQ"),
        ("https://app.loxo.co/job/MzI3MjAtanU5dTMzcXo4ZWFqOWhxcw==/form?source_type=app",
         "https://app.loxo.co/job/MzI3MjAtanU5dTMzcXo4ZWFqOWhxcw==?t=1793042439183"),
        ("https://ekaw.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/175901/apply/section/1",
         "https://ekaw.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/175901"),
        ("https://vectorus.clearcompany.com/careers/jobs/aa7392ce-a800-7ece-3f53-b48f1d159b74/apply/092e31fa-f1bd",
         "https://vectorus.clearcompany.com/careers/jobs/aa7392ce-a800-7ece-3f53-b48f1d159b74"),
        ("https://itshoulders.betterteam.com/data-engineer/apply/standard", "https://itshoulders.betterteam.com/data-engineer"),
        ("https://recruiting.paylocity.com/Recruiting/Jobs/Apply/4555860",
         "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4555860"),
        ("https://recruiting.paylocity.com/Recruiting/jobs/Apply/4609216",
         "https://recruiting.paylocity.com/recruiting/jobs/Details/4609216/TriVista-Business-Group-Inc/Senior-AI-Engineer"),
        ("https://welkinhealthinc.applytojob.com/apply/confirm/n9BuXsWzAX",
         "https://welkinhealthinc.applytojob.com/apply/n9BuXsWzAX/Senior-Software-Engineer"),
        ("https://vispero.rec.pro.ukg.net/FRE1500FRDS/JobBoard/bdb17a81-bb2f/OpportunityApply?opportunityId=f4848774-63b1",
         "https://vispero.rec.pro.ukg.net/FRE1500FRDS/JobBoard/bdb17a81-bb2f/OpportunityDetail?opportunityId=f4848774-63b1"),
        ("https://redriver.rec.pro.ukg.net/RED1507RDRIV/JobBoard/66584047/OpportunityApply?opportunityId=d6908c80-d7c7",
         "https://redriver.rec.pro.ukg.net/RED1507RDRIV/JobBoard/66584047/OpportunityDetail?opportunityId=d6908c80-d7c7&jr_id=6"),
        ("https://recruitingbypaycor.com/career/SubmitQuestionnaire.action?clientId=8a78&id=8a7885a8&specialization=",
         "https://recruitingbypaycor.com/career/JobIntroduction.action?clientId=8a78&id=8a7885a8&lang=en"),
        ("https://recruitingbypaycor.com/career/SubmitVevraaForm.action?clientId=8a78c6&id=8a78ff",
         "https://recruitingbypaycor.com/career/JobIntroduction.action?clientId=8a78c6&id=8a78ff&lang=en"),
        ("https://highmarkhealth.wd1.myworkdayjobs.com/highmark/job/PA%2C-Working-at-Home---Pennsylvania/Software-Engineer_J281003",
         "https://highmarkhealth.wd1.myworkdayjobs.com/highmark/job/PA-Working-at-Home---Pennsylvania/Software-Engineer_J281003"),
        ("https://ats.rippling.com/en-GB/ozmocareers/jobs/d42d19d5-cf8d-43fc-a0eb-dce35f5bb5bf",
         "https://ats.rippling.com/ozmocareers/jobs/d42d19d5-cf8d-43fc-a0eb-dce35f5bb5bf"),
        ("https://jobs.workable.com/view/bn4Vdj56ioJvybihdFA1mT/remote-azure-data-engineer-in-chicago-at-tiger-analytics-inc.",
         "https://jobs.workable.com/view/bn4Vdj56ioJvybihdFA1mT/remote-azure-data-engineer-in-chicago-at-tiger-analytics-inc"),
        # SmartRecruiters job pages: the title slug and company case vary (the API's postingUrl ends "…-engineer-").
        ("https://jobs.smartrecruiters.com/Vericast/744000162822809-senior-data-engineer-",
         "https://jobs.smartrecruiters.com/vericast/744000162822809-senior-data-engineer?utm_source=remoteyeah.com"),
        ("https://jobs.smartrecruiters.com/FusionConsulting/744000137602589",
         "https://jobs.smartrecruiters.com/FusionConsulting/744000137602589-sr-engineer?ref=remoteyeah.com"),
    ],
)
def test_apply_step_and_posting_clean_to_the_same_job(bid_url, resume_url):
    assert normalize_url(bid_url) == normalize_url(resume_url)
    assert job_url_key(bid_url) == job_url_key(resume_url)
    for url in (bid_url, resume_url):
        once = normalize_url(url)
        assert normalize_url(once) == once


@pytest.mark.parametrize(
    "url",
    [
        # Two real segments stay: listing and form pages that are the whole site are not cut down.
        "https://acme.com/careers/new",
        "https://acme.com/jobs/apply",
        "https://acme.com/a/new",
        # "new", "a" or "apply" as part of a job's own path, not a trailing step.
        "https://acme.com/jobs/new-grad-engineer",
        "https://acme.com/careers/applications/123",
        "https://acme.applytojob.com/apply/ART1tJYpSi",
        "https://recruiting.ultipro.com/ACM1000/JobBoard/abc/OpportunityDetail?opportunityId=9f3c",
    ],
)
def test_job_pages_are_left_alone(url):
    assert normalize_url(url) == url


@pytest.mark.parametrize(
    "bid_url, other_job",
    [
        # Same board, different jobs: never merged.
        ("https://vectorus.clearcompany.com/careers/jobs/aa73/apply/092e", "https://vectorus.clearcompany.com/careers/jobs/bb84"),
        ("https://recruiting.paylocity.com/Recruiting/Jobs/Apply/4555860",
         "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4555861"),
        ("https://x.rec.pro.ukg.net/T1/JobBoard/b1/OpportunityApply?opportunityId=o1",
         "https://x.rec.pro.ukg.net/T1/JobBoard/b1/OpportunityDetail?opportunityId=o2"),
    ],
)
def test_different_jobs_stay_apart(bid_url, other_job):
    assert normalize_url(bid_url) != normalize_url(other_job)

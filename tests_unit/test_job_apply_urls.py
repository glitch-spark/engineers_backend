"""URL cleaning, ported from job_url_filter (tests/test_normalize.py) with its case table. Differences: the
scheme is kept, and only the scheme's default port is dropped."""
import pytest

from app.job_apply.urls import normalize_all, normalize_url


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

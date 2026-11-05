"""Check resume storage against the configured bucket (AWS S3 or Backblaze B2 via AWS_S3_ENDPOINT_URL).

Run from engineers_backend with the storage vars in .env:  python -m scripts.b2_smoke_test
Writes, reads, copies and deletes two small objects under _smoke/; prints a presigned link to try in a browser.
"""
import time

from app.config import get_settings
from app.resume_service import _s3_client, generate_presigned_get


def main() -> None:
    s = get_settings()
    bucket = s.AWS_S3_BUCKET
    print(f"endpoint={s.AWS_S3_ENDPOINT_URL or 'AWS'} region={s.AWS_REGION} bucket={bucket}")
    s3 = _s3_client()
    key = f"_smoke/{int(time.time())}/Resume.pdf"
    copy = key.replace("Resume.pdf", "Copy.pdf")
    body = b"%PDF-1.4\n% smoke test\n"

    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/pdf", ServerSideEncryption="AES256")
    print("put_object     ok")
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == body
    print("get_object     ok")
    s3.copy_object(Bucket=bucket, Key=copy, CopySource={"Bucket": bucket, "Key": key})
    print("copy_object    ok")
    print("presigned link (should download as Smoke Test.pdf for 15 min):")
    print("  " + generate_presigned_get(key, download_filename="Smoke Test.pdf"))
    input("Open the link, then press Enter to delete the test objects... ")
    s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key}, {"Key": copy}], "Quiet": True})
    print("delete_objects ok")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Simple AWS Batch orchestrator for read-sizer.
Reuses existing generate_samplesheet.py for samplesheet generation.

Usage:
  python submit_batch_jobs.py --sample-sheet samples.csv --job-queue Q --job-definition D
  python submit_batch_jobs.py --bucket B --delivery D --job-queue Q --job-definition D
"""

import argparse
import boto3
import csv
import shlex
import sys
import time
from pathlib import Path
from typing import List, Dict, Tuple
from datetime import datetime
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

# Import existing samplesheet generation logic
try:
    from scripts.generate_samplesheet import generate_samplesheet
except ImportError:
    print("Error: Could not import scripts.generate_samplesheet", file=sys.stderr)
    print("Make sure scripts/generate_samplesheet.py exists", file=sys.stderr)
    sys.exit(1)


def parse_s3_uri(uri: str) -> Tuple[str, str]:
    """Parse an s3://bucket/key URI into (bucket, key)."""
    if not uri.startswith("s3://"):
        raise ValueError(f"Expected s3:// URI, got {uri!r}")
    bucket, _, key = uri[5:].partition("/")
    if not bucket or not key:
        raise ValueError(f"Invalid S3 URI (missing bucket or key): {uri!r}")
    return bucket, key


def validate_sample_inputs(
    samples: List[Dict],
    s3_client=None,
    no_sign_request: bool = False,
) -> None:
    """Verify every sample's fastq_1 and fastq_2 inputs exist before submitting Batch jobs.

    Raises FileNotFoundError naming any missing or inaccessible S3/local inputs so
    the pipeline fails closed instead of silently skipping nonexistent files inside
    bash process substitution.
    """
    missing: List[str] = []
    for idx, sample in enumerate(samples):
        sample_id = (sample.get("id") or f"row-{idx}").strip()
        for col in ("fastq_1", "fastq_2"):
            path = (sample.get(col) or "").strip()
            if not path:
                missing.append(f"{sample_id} ({col}): <empty>")
                continue
            if path.startswith("s3://"):
                try:
                    bucket, key = parse_s3_uri(path)
                except ValueError:
                    missing.append(f"{sample_id} ({col}): {path} (invalid S3 URI)")
                    continue
                if s3_client is None:
                    cfg = Config(signature_version=UNSIGNED) if no_sign_request else None
                    s3_client = boto3.client("s3", config=cfg)
                try:
                    s3_client.head_object(Bucket=bucket, Key=key)
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "Error")
                    missing.append(f"{sample_id} ({col}): {path} ({code})")
            else:
                if not Path(path).is_file():
                    missing.append(f"{sample_id} ({col}): {path}")

    if missing:
        raise FileNotFoundError(
            "Input file(s) do not exist or are inaccessible:\n  "
            + "\n  ".join(missing)
        )


def _container_preflight_check(path: str, no_sign_request: bool = False) -> str:
    """Return a shell command verifying input existence before process substitution."""
    quoted = shlex.quote(path)
    if path.startswith("s3://"):
        flag = " --no-sign-request" if no_sign_request else ""
        return f"aws s3 ls{flag} {quoted} > /dev/null"
    return f"test -f {quoted}"


def submit_batch_job(batch_client, sample: Dict, job_queue: str, job_definition: str,
                     chunk_size: int, zstd_level: int, dry_run: bool = False,
                     no_sign_request: bool = False) -> str:
    """Submit a single sizer job to AWS Batch."""
    cp_flag = " --no-sign-request" if no_sign_request else ""
    preflight_1 = _container_preflight_check(sample['fastq_1'], no_sign_request)
    preflight_2 = _container_preflight_check(sample['fastq_2'], no_sign_request)
    command = [
        "/bin/bash", "-c",
        f"""set -e -o pipefail
        {preflight_1}
        {preflight_2}
        sizer.sh -s /usr/local/bin/split_interleave_fastqs \\
            -u /sequence_tools/compress_upload.sh \\
            -c {chunk_size} \\
            -l {zstd_level} \\
            <(aws s3 cp{cp_flag} {shlex.quote(sample['fastq_1'])} - | gunzip) \\
            <(aws s3 cp{cp_flag} {shlex.quote(sample['fastq_2'])} - | gunzip) \\
            {shlex.quote(sample['id'])} \\
            {shlex.quote(sample['outdir'] + sample['id'])}
        """
    ]

    if dry_run:
        print(f"[DRY RUN] Would submit job: sizer-{sample['id']}")
        print(f"  Command: {command[2].strip()}")
        return f"dry-run-{sample['id']}"

    response = batch_client.submit_job(
        jobName=f"sizer-{sample['id']}",
        jobQueue=job_queue,
        jobDefinition=job_definition,
        containerOverrides={"command": command}
    )
    return response['jobId']


def monitor_jobs(batch_client, job_tracker: Dict, max_retries: int,
                job_queue: str, job_definition: str, chunk_size: int, zstd_level: int,
                no_sign_request: bool = False) -> List[str]:
    """
    Monitor running jobs and retry failures.

    job_tracker format: {job_id: {"sample": sample_dict, "retry_count": int}}

    Returns:
        List of sample IDs that failed permanently.
    """
    failed_permanently = []

    while job_tracker:
        time.sleep(10)

        # Batch describe_jobs calls (up to 100 at a time)
        job_ids = list(job_tracker.keys())
        for i in range(0, len(job_ids), 100):
            if i > 0:
                time.sleep(0.25)
            batch_ids = job_ids[i:i+100]
            response = batch_client.describe_jobs(jobs=batch_ids)

            for job_info in response['jobs']:
                job_id = job_info['jobId']
                sample_id = job_tracker[job_id]['sample']['id']
                status = job_info['status']

                if status == 'SUCCEEDED':
                    print(f"✓ {sample_id} succeeded")
                    del job_tracker[job_id]

                elif status == 'FAILED':
                    retry_count = job_tracker[job_id]['retry_count']
                    reason = job_info.get('statusReason', 'Unknown')

                    if retry_count < max_retries:
                        # Retry with detailed logging
                        print(f"↻ Retrying {sample_id} (attempt {retry_count + 2}/{max_retries + 1}) - Reason: {reason}")
                        sample = job_tracker[job_id]['sample']
                        new_job_id = submit_batch_job(
                            batch_client, sample, job_queue, job_definition,
                            chunk_size, zstd_level, no_sign_request=no_sign_request
                        )
                        job_tracker[new_job_id] = {"sample": sample, "retry_count": retry_count + 1}
                        del job_tracker[job_id]
                    else:
                        print(f"✗ {sample_id} failed permanently after {max_retries + 1} attempts - Reason: {reason}")
                        failed_permanently.append(sample_id)
                        del job_tracker[job_id]

    return failed_permanently


def main():
    parser = argparse.ArgumentParser(
        description="Submit and monitor AWS Batch jobs for read-sizer pipeline"
    )

    # Input options: either sample sheet OR bucket/delivery
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--sample-sheet", help="Path to existing sample sheet CSV")
    input_group.add_argument("--bucket", help="S3 bucket name (for auto-generation)")

    # Required for auto-generation mode
    parser.add_argument("--delivery", help="Delivery folder name (required with --bucket)")

    # Required for all modes
    parser.add_argument("--job-queue", required=True, help="AWS Batch job queue name")
    parser.add_argument("--job-definition", required=True, help="AWS Batch job definition name")

    # Optional parameters
    parser.add_argument("--outdir", help="Custom output directory (default: s3://bucket/delivery/siz/)")
    parser.add_argument("--chunk-size", type=int, default=1000000, help="Chunk size (default: 1000000)")
    parser.add_argument("--zstd-level", type=int, default=15, help="Zstd compression level (default: 15)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retry attempts per job (default: 3)")
    parser.add_argument("--dry-run", action="store_true", help="Print jobs without submitting")
    parser.add_argument(
        "--no-sign-request",
        action="store_true",
        help="Use --no-sign-request for S3 operations (for public repositories)"
    )
    parser.add_argument(
        "--ignore-existing",
        action="store_true",
        help="Ignore existing SIZ files; process all FASTQ pairs"
    )

    args = parser.parse_args()

    # Validate bucket/delivery pairing
    if args.bucket and not args.delivery:
        parser.error("--delivery is required when using --bucket")

    # Get samples
    print(f"READ-SIZER PIPELINE - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    if args.sample_sheet:
        print(f"Loading samples from {args.sample_sheet}...")
        with open(args.sample_sheet) as f:
            samples = list(csv.DictReader(f))
    else:
        print(f"Generating samplesheet from s3://{args.bucket}/{args.delivery}...")
        samples = generate_samplesheet(
            args.bucket, args.delivery, args.outdir,
            args.ignore_existing, args.no_sign_request
        )

    if not samples:
        print("No samples to process")
        sys.exit(0)

    try:
        validate_sample_inputs(samples, no_sign_request=args.no_sign_request)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(samples)} sample(s) to process\n")

    # Submit all jobs
    batch_client = boto3.client('batch')
    job_tracker = {}

    print("Submitting jobs...")
    for sample in samples:
        job_id = submit_batch_job(
            batch_client, sample, args.job_queue, args.job_definition,
            args.chunk_size, args.zstd_level, args.dry_run, args.no_sign_request
        )
        job_tracker[job_id] = {"sample": sample, "retry_count": 0}
        if not args.dry_run:
            print(f"  Submitted {sample['id']} -> {job_id}")

    if args.dry_run:
        print("\nDry run complete - no jobs were actually submitted")
        sys.exit(0)

    print(f"\nMonitoring {len(job_tracker)} job(s)...\n")
    total_jobs = len(job_tracker)

    # Monitor and retry
    failed = monitor_jobs(
        batch_client, job_tracker, args.max_retries,
        args.job_queue, args.job_definition, args.chunk_size, args.zstd_level,
        no_sign_request=args.no_sign_request
    )

    # Print summary
    succeeded = total_jobs - len(failed)
    if failed:
        print(f"\n{succeeded} succeeded, {len(failed)} failed permanently")
        sys.exit(1)
    else:
        print(f"\nAll {succeeded} job(s) completed successfully!")


if __name__ == "__main__":
    main()

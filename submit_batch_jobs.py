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
import sys
import time
from typing import List, Dict
from datetime import datetime

# Import existing samplesheet generation logic
try:
    from scripts.generate_samplesheet import generate_samplesheet
except ImportError:
    print("Error: Could not import scripts.generate_samplesheet", file=sys.stderr)
    print("Make sure scripts/generate_samplesheet.py exists", file=sys.stderr)
    sys.exit(1)


def submit_batch_job(batch_client, sample: Dict, job_queue: str, job_definition: str,
                     chunk_size: int, zstd_level: int, dry_run: bool = False) -> str:
    """Submit a single sizer job to AWS Batch."""
    command = [
        "/bin/bash", "-c",
        f"""
        sizer.sh -s /usr/local/bin/split_interleave_fastqs \\
            -u /sequence_tools/compress_upload.sh \\
            -c {chunk_size} \\
            -l {zstd_level} \\
            <(aws s3 cp {sample['fastq_1']} - | gunzip) \\
            <(aws s3 cp {sample['fastq_2']} - | gunzip) \\
            {sample['id']} \\
            {sample['outdir']}{sample['id']}
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
                job_queue: str, job_definition: str, chunk_size: int, zstd_level: int):
    """
    Monitor running jobs and retry failures.

    job_tracker format: {job_id: {"sample": sample_dict, "retry_count": int}}
    """
    while job_tracker:
        time.sleep(5)

        # Batch describe_jobs calls (up to 100 at a time)
        job_ids = list(job_tracker.keys())
        for i in range(0, len(job_ids), 100):
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
                            chunk_size, zstd_level
                        )
                        job_tracker[new_job_id] = {"sample": sample, "retry_count": retry_count + 1}
                        del job_tracker[job_id]
                    else:
                        print(f"✗ {sample_id} failed permanently after {max_retries + 1} attempts - Reason: {reason}")
                        del job_tracker[job_id]


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
    parser.add_argument("--outdir", help="Custom output directory (default: infer from raw path)")
    parser.add_argument("--chunk-size", type=int, default=1000000, help="Chunk size (default: 1000000)")
    parser.add_argument("--zstd-level", type=int, default=5, help="Zstd compression level (default: 5)")
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

    print(f"Found {len(samples)} sample(s) to process\n")

    # Submit all jobs
    batch_client = boto3.client('batch')
    job_tracker = {}

    print("Submitting jobs...")
    for sample in samples:
        job_id = submit_batch_job(
            batch_client, sample, args.job_queue, args.job_definition,
            args.chunk_size, args.zstd_level, args.dry_run
        )
        job_tracker[job_id] = {"sample": sample, "retry_count": 0}
        if not args.dry_run:
            print(f"  Submitted {sample['id']} -> {job_id}")

    if args.dry_run:
        print("\nDry run complete - no jobs were actually submitted")
        sys.exit(0)

    print(f"\nMonitoring {len(job_tracker)} job(s)...\n")

    # Monitor and retry
    monitor_jobs(
        batch_client, job_tracker, args.max_retries,
        args.job_queue, args.job_definition, args.chunk_size, args.zstd_level
    )

    print("\nAll jobs completed!")


if __name__ == "__main__":
    main()

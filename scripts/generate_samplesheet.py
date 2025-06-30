#!/usr/bin/env python3
import argparse
import subprocess
import csv
import sys
import re


def list_s3_files(s3_path, allow_missing=False, no_sign_request=False):
    """List files at an S3 path using the AWS CLI."""
    try:
        cmd = ["aws", "s3", "ls", s3_path]
        if no_sign_request:
            cmd.append("--no-sign-request")
            
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True
        )
        files = []
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                filename = parts[-1]
                files.append(filename)
        return files
    except subprocess.CalledProcessError as e:
        if allow_missing:
            sys.stderr.write(
                f"Warning: Could not list {s3_path}. Assuming directory is missing.\n"
            )
            return []
        else:
            sys.stderr.write(f"Error listing {s3_path}: {e}\n")
            sys.exit(1)


def infer_output_dir(fastq_path):
    """Infer output directory by replacing 'raw' with 'siz' in the path"""
    # Replace /raw/ with /siz/
    output_dir = fastq_path.replace("/raw/", "/siz/")
    # Remove the filename to get just the directory
    output_dir = re.sub(r"/[^/]+$", "/", output_dir)
    return output_dir


def main():
    parser = argparse.ArgumentParser(
        description="Generate sample_sheet.csv from raw FASTQ files and existing SIZ files"
    )
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--delivery", required=True, help="Delivery folder name")
    parser.add_argument(
        "--outdir", help="Custom output directory (default: infer from raw path)"
    )
    parser.add_argument(
        "--output",
        default="sample_sheet.csv",
        help="Output CSV file (default: sample_sheet.csv)",
    )
    parser.add_argument(
        "--no-sign-request",
        action="store_true",
        help="Use --no-sign-request flag with AWS CLI commands (for public repositories)"
    )
    parser.add_argument(
        "--ignore-existing",
        action="store_true",
        help="Ignore existing SIZ files; sample sheet will include all FASTQ pairs"
    )
    args = parser.parse_args()

    # Construct S3 paths
    raw_dir = f"s3://{args.bucket}/{args.delivery}/raw/"
    siz_dir = f"s3://{args.bucket}/{args.delivery}/siz/"

    # List files in raw and siz directories
    raw_files = list_s3_files(raw_dir, allow_missing=False, no_sign_request=args.no_sign_request)
    siz_files = list_s3_files(siz_dir, allow_missing=True, no_sign_request=args.no_sign_request)

    # Build dictionary of ids from raw files
    ids = {}
    for f in raw_files:
        if f.endswith("_1.fastq.gz"):
            id = f[: -len("_1.fastq.gz")]
            ids.setdefault(id, {})["R1"] = raw_dir + f
        elif f.endswith("_2.fastq.gz"):
            id = f[: -len("_2.fastq.gz")]
            ids.setdefault(id, {})["R2"] = raw_dir + f

    # If we're ignoring existing, we don't need to compute processed ids
    if not args.ignore_existing:
        processed_ids = set()
        for f in siz_files:
            if "_chunk" in f:
                id = f.partition("_chunk")[0]
                processed_ids.add(id)

    # Write sample sheet
    with open(args.output, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["id", "fastq_1", "fastq_2", "outdir"])

        for id, reads in ids.items():
            if not args.ignore_existing and id in processed_ids:
                continue

            if "R1" in reads and "R2" in reads:
                # Use provided outdir if available
                if args.outdir:
                    outdir = args.outdir
                # Otherwise infer from fastq_1 path
                else:
                    outdir = infer_output_dir(reads["R1"])

                writer.writerow([id, reads["R1"], reads["R2"], outdir])
            else:
                sys.stderr.write(f"Warning: Incomplete pair for id {id}\n")

    print(f"Sample sheet written to {args.output}")


if __name__ == "__main__":
    main()
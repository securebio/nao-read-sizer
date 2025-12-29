# read-SIZer

## Overview

This repository contains a Python pipeline for converting paired gziped-FASTQ files to [SIZ](#siz-spec) (**s**plit, **i**nterleaved, **z**std-compressed) format. The pipeline is designed for wide parallelism:
* Each forward-reverse pair of input `.fastq.gz` files is processed in parallel.
* Within each file pair, zstd compression jobs run in parallel.
* All data movement to and from S3 is by streaming, and data movement on a given machine is within memory.

Note that the pipeline structure is specialized for the NAO use case:
* Input `.fastq.gz` files are stored in S3.
* Output `.fastq.zst` files are uploaded to S3.
* Automatic [sample sheet generation](#automatically-generated-sample-sheet) assumes a NAO-like bucket structure. (Described [below](#automatically-generated-sample-sheet).)
* Default chunk size and compression level parameters match NAO standards. (1 million read pairs and `-15`, respectively.)
* [Running on](#running-with-batch) AWS Batch is recommended.

That said, the repository contains no private NAO information and others are welcome to use it.

## Prerequisites and installation

### Installation

1. Clone the repository:
    ```bash
    git clone git@github.com:naobservatory/read-sizer.git
    cd read-sizer
    ```
2. Install [Python 3.8+](https://www.python.org/downloads/)
3. Install [UV](https://docs.astral.sh/uv/getting-started/installation/) for dependency management:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```
4. Install dependencies:
    ```bash
    uv sync
    ```
5. Install the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)

### AWS access

Configure AWS credentials in `~/.aws/credentials`:

```ini
[default]
region=us-east-1
aws_access_key_id = <ACCESS_KEY_ID>
aws_secret_access_key = <SECRET_ACCESS_KEY>
```

> **Note**: If you encounter `AccessDenied` errors, export your credentials as environment variables:
> ```bash
> eval "$(aws configure export-credentials --format env)"
> ```

You'll need S3 permissions to read input data and write output data. Automatically generating a sample sheet requires S3 list permissions in the relevant bucket.

## Usage

There are two ways to specify inputs and outputs to the SIZer, described below:
* Sample sheet CSV (most flexible)
* `--bucket` and `--delivery` parameters, which are used to automatically generate a sample sheet

In both cases, input data must be stored in S3 in `.fastq.gz` format, with forward and reverse reads in separate files, identically ordered.
* **Warning:** The workflow doesn't support input FASTQs with hard line breaks for long sequences; each FASTQ record must be exactly four lines.

### Sample sheet

You can provide a CSV sample sheet with the four columns:
1. `id`: Sample ID (string)
2. `fastq_1`: S3 path to forward reads file
3. `fastq_2`: S3 path to reverse reads file
4. `outdir`: S3 output base path

e.g.
```csv
id,fastq_1,fastq_2,outdir
naboo,s3://bucket/raw/naboo_lane1_1.fastq.gz,s3://bucket/raw/naboo_lane1_2.fastq.gz,s3://output-bucket/
yavin,s3://bucket/raw/yavin_lane1_1.fastq.gz,s3://bucket/raw/yavin_lane1_2.fastq.gz,s3://output-bucket/
```
The pipeline is just a parallel for loop: for each row of the sample sheet, SIZer the reads in `fastq_1` and `fastq_2` to files `<outdir><id>_chunkNNNNNN.fastq.zst`.
There's no requirement that all the inputs or outputs are in the same bucket -- just make sure you have the relevant S3 permissions.

Note how `id` is directly appended to `outdir`. This means output _directories_ should end with a slash. Non-slash-terminated `outdirs` like `s3://a/b` will yield output files like `s3://a/b<id>_chunkNNNNNN.fastq.zst`.


Once you have a sample sheet you're happy with, run the pipeline:
```bash
uv run python submit_batch_jobs.py \
  --sample-sheet <path-to-sample-sheet> \
  --job-queue <your-batch-queue> \
  --job-definition <your-job-definition>
```

### Automatically generated sample sheet

If no sample sheet is provided but `--bucket` and `--delivery` are specified, a sample sheet will be automatically generated using `scripts/generate_samplesheet.py` by:
1. Scanning the input directory `s3://<bucket>/<delivery>/raw/` for `.fastq.gz` files
2. Identifying pairs of files that need processing (files that don't already have a processed version in the output directory `s3://<bucket>/<delivery>/siz/`)
3. The output directory for the SIZ files is inferred from the input paths if `--outdir` is not specified.

Automatic sample sheet generation assumes:
* Raw data are in `s3://<bucket>/<delivery>/raw/<id>_{1,2}.fastq.gz`
* You want to SIZer all raw files in the corresponding delivery
* You will specify `--outdir`, or you want outputs in `s3://<bucket>/<delivery>/siz/<id>_chunkNNNNNN.fastq.zst`.

If some of these conditions don't hold, it may still be helpful to execute `generate_samplesheet.py` outside of the pipeline to generate a sample sheet you can modify.

To run the pipeline with automatic sample sheet generation:
```bash
uv run python submit_batch_jobs.py \
  --bucket my-data-bucket \
  --delivery delivery-to-siz \
  --job-queue <your-batch-queue> \
  --job-definition <your-job-definition>
```

#### Forcing regeneration of existing SIZ files

By default, the automatic sample sheet generation skips FASTQ pairs that already have corresponding SIZ files in the output directory. This prevents unnecessary reprocessing of already-SIZed data.

However, if you suspect existing SIZ files are corrupted or incomplete (e.g., from a previous failed run), you can force regeneration of all SIZ files using the `--ignore-existing` flag:

```bash
uv run python submit_batch_jobs.py \
  --bucket my-data-bucket \
  --delivery delivery-to-siz \
  --job-queue <your-batch-queue> \
  --job-definition <your-job-definition> \
  --ignore-existing
```

**Warning**: This will regenerate SIZ files for _all_ FASTQ pairs in the delivery, overwriting any existing SIZ files in the output directory. Use with caution.

### Running with AWS Batch

Running the pipeline with AWS Batch is recommended:
* The pipeline is trivially parallelizable across input file pairs, and Batch is a convenient way to temporarily spin up lots of parallel resources.
* If you've configured your Batch environment to use spot instances, running with Batch can also be cost saving.

#### Setting up AWS Batch

You'll need to create an AWS Batch job definition that specifies the compute resources for processing. The recommended configuration for high-performance processing is:

```bash
aws batch register-job-definition \
  --job-definition-name sizer-job-def \
  --type container \
  --container-properties '{
    "image": "public.ecr.aws/q0n1c7g8/nao/read-sizer:latest",
    "vcpus": 64,
    "memory": 122880,
    "jobRoleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/YourBatchJobRole"
  }' \
  --timeout attemptDurationSeconds=10800
```

**Note**: Memory is in MiB. `122880 MiB = 120 GiB`. Replace `YOUR_ACCOUNT_ID` with your AWS account ID and ensure your job role has S3 read/write permissions.

For more details on the AWS Batch setup, see [migrate_off_nextflow.md](migrate_off_nextflow.md).

#### Custom parameters

The pipeline supports several optional parameters:

```bash
uv run python submit_batch_jobs.py \
  --sample-sheet samples.csv \
  --job-queue <your-batch-queue> \
  --job-definition <your-job-definition> \
  --chunk-size 500000 \
  --zstd-level 10 \
  --max-retries 5
```

Available options:
* `--chunk-size`: Number of read pairs per chunk (default: 1000000)
* `--zstd-level`: Zstandard compression level (default: 5)
* `--max-retries`: Maximum retry attempts for failed jobs (default: 3)
* `--dry-run`: Print jobs without submitting them (useful for testing)


# SIZ: **S**plit, **i**nterleaved, **z**std compressed

SIZ files are a kind of [Zstandard](https://facebook.github.io/zstd/)\-compressed FASTQ file with extra guarantees. All SIZ files yield valid FASTQ files when decompressed. 

## Why store data in SIZ format?
* _Splitting_ large datasets into chunks of bounded size is helpful for parallelism, e.g. we can search for reads in a large dataset by having separate processes search within each chunk.
* _Interleaving_ paired-end reads allows both forward and reverse reads to be streamed via stdin and stdout, which can be super handy for efficient streaming workflows.
* _Zstandard_ compression dominates the more-common gzip on the tradeoff between compression speed and compression ratio. It also decompresses quickly.

## SIZ spec

SIZ files have the following properties:

* SIZ files represent paired-end short read data.
* Paired reads are interleaved: `<fwd1><rev1><fwd2><rev2>...`
* SIZ files represent datasets, such as larger FASTQ files, entire samples, etc., split into chunks.
  * SIZ files from the same source are named `<prefix>_chunkUVWXYZ.fastq.zst`, where `UVWXYZ` is a fixed-width (6 decimal digits) 0-indexed counter.
  * When splitting a dataset into SIZ files, each SIZ file contains exactly 1 million read pairs, except the last (highest index) file may contain fewer in the likely case that the dataset’s total size is not a perfect multiple of 1 million read pairs.
    * Note that after transforming a SIZ chunk (e.g. adapter trimming or deduplication) you may find yourself with a SIZ file that has fewer or (rarely) more than 1 million read pairs.
  * For example, paired FASTQ files `cool_data_1.fastq` and `cool_data_2.fastq` with 2.7 million read pairs would be packaged into SIZ files (note using a prefix besides cool\_data is valid):
    * `cool_data_chunk000000.fastq.zst` (1 million read pairs)
    * `cool_data_chunk000001.fastq.zst` (1 million read pairs)
    * `cool_data_chunk000002.fastq.zst` (.7 million read pairs)
* SIZ files have extension `.fastq.zst`
* When decompressed, SIZ files yield FASTQ files with exactly 4 lines per FASTQ record (so 8 lines per read pair), i.e. sequences are not broken across lines.

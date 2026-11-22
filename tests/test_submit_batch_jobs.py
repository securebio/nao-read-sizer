"""Simple tests for submit_batch_jobs.py"""
import sys
import pytest
from botocore import UNSIGNED
from botocore.exceptions import ClientError
from pathlib import Path
from unittest.mock import MagicMock, patch
import submit_batch_jobs
from submit_batch_jobs import (
    monitor_jobs,
    parse_s3_uri,
    submit_batch_job,
    validate_sample_inputs,
)


class TestSubmitBatchJob:
    def test_submits_job_with_correct_parameters(self):
        mock_batch = MagicMock()
        mock_batch.submit_job.return_value = {'jobId': 'job-123'}

        sample = {
            'id': 'test-sample',
            'fastq_1': 's3://bucket/raw/test_1.fastq.gz',
            'fastq_2': 's3://bucket/raw/test_2.fastq.gz',
            'outdir': 's3://bucket/siz/'
        }

        job_id = submit_batch_job(mock_batch, sample, 'queue', 'job-def', 1000000, 15)

        assert job_id == 'job-123'
        mock_batch.submit_job.assert_called_once()
        call_args = mock_batch.submit_job.call_args[1]
        assert call_args['jobName'] == 'sizer-test-sample'
        assert call_args['jobQueue'] == 'queue'
        assert call_args['jobDefinition'] == 'job-def'

    def test_dry_run_returns_mock_job_id(self):
        mock_batch = MagicMock()
        sample = {
            'id': 'test-sample',
            'fastq_1': 's3://bucket/raw/test_1.fastq.gz',
            'fastq_2': 's3://bucket/raw/test_2.fastq.gz',
            'outdir': 's3://bucket/siz/'
        }

        job_id = submit_batch_job(mock_batch, sample, 'queue', 'job-def', 1000000, 5, dry_run=True)

        assert job_id == 'dry-run-test-sample'
        mock_batch.submit_job.assert_not_called()


class TestMonitorJobs:
    def test_retries_failed_job(self):
        mock_batch = MagicMock()
        # First call: job failed
        # Second call: new job succeeded
        mock_batch.describe_jobs.side_effect = [
            {'jobs': [{'status': 'FAILED', 'statusReason': 'SpotInterruption', 'jobId': 'job-123'}]},
            {'jobs': [{'status': 'SUCCEEDED', 'jobId': 'job-456'}]}
        ]
        mock_batch.submit_job.return_value = {'jobId': 'job-456'}

        sample = {
            'id': 'test',
            'fastq_1': 's3://bucket/raw/test_1.fastq.gz',
            'fastq_2': 's3://bucket/raw/test_2.fastq.gz',
            'outdir': 's3://bucket/siz/'
        }
        job_tracker = {
            'job-123': {'sample': sample, 'retry_count': 0}
        }

        with patch('time.sleep'):
            monitor_jobs(mock_batch, job_tracker, 3, 'queue', 'def', 1000000, 15)

        # Should have retried once and then succeeded
        assert mock_batch.submit_job.call_count == 1
        assert len(job_tracker) == 0

    def test_stops_retrying_after_max_attempts(self):
        mock_batch = MagicMock()
        mock_batch.describe_jobs.return_value = {
            'jobs': [{'status': 'FAILED', 'statusReason': 'Error', 'jobId': 'job-123'}]
        }

        sample = {
            'id': 'test',
            'fastq_1': 's3://bucket/raw/test_1.fastq.gz',
            'fastq_2': 's3://bucket/raw/test_2.fastq.gz',
            'outdir': 's3://bucket/siz/'
        }
        job_tracker = {
            'job-123': {'sample': sample, 'retry_count': 3}  # Already at max
        }

        with patch('time.sleep'):
            monitor_jobs(mock_batch, job_tracker, 3, 'queue', 'def', 1000000, 15)

        # Should not retry, just remove from tracker
        mock_batch.submit_job.assert_not_called()
        assert len(job_tracker) == 0


class TestValidateSampleInputs:
    def test_parse_s3_uri(self):
        assert parse_s3_uri("s3://my-bucket/path/to/file_1.fastq.gz") == (
            "my-bucket",
            "path/to/file_1.fastq.gz",
        )
        with pytest.raises(ValueError, match="Invalid S3 URI"):
            parse_s3_uri("s3://only-bucket/")

    def test_passes_when_all_s3_inputs_exist(self):
        mock_s3 = MagicMock()
        samples = [
            {
                "id": "s1",
                "fastq_1": "s3://bucket/raw/s1_1.fastq.gz",
                "fastq_2": "s3://bucket/raw/s1_2.fastq.gz",
                "outdir": "s3://bucket/siz/",
            }
        ]
        validate_sample_inputs(samples, s3_client=mock_s3)
        assert mock_s3.head_object.call_count == 2
        mock_s3.head_object.assert_any_call(Bucket="bucket", Key="raw/s1_1.fastq.gz")
        mock_s3.head_object.assert_any_call(Bucket="bucket", Key="raw/s1_2.fastq.gz")

    def test_raises_when_s3_input_missing(self):
        mock_s3 = MagicMock()
        not_found = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        mock_s3.head_object.side_effect = [None, not_found]
        samples = [
            {
                "id": "missing-sample",
                "fastq_1": "s3://bucket/raw/s1_1.fastq.gz",
                "fastq_2": "s3://bucket/raw/missing_2.fastq.gz",
                "outdir": "s3://bucket/siz/",
            }
        ]
        with pytest.raises(FileNotFoundError, match="missing-sample.*missing_2.fastq.gz"):
            validate_sample_inputs(samples, s3_client=mock_s3)

    def test_raises_on_empty_path_or_missing_local_file(self, tmp_path: Path):
        f1 = tmp_path / "ok_1.fastq.gz"
        f2 = tmp_path / "ok_2.fastq.gz"
        f1.write_bytes(b"")
        f2.write_bytes(b"")
        validate_sample_inputs(
            [{"id": "local-ok", "fastq_1": str(f1), "fastq_2": str(f2)}]
        )
        with pytest.raises(FileNotFoundError, match="local-bad.*missing.fastq.gz"):
            validate_sample_inputs(
                [
                    {
                        "id": "local-bad",
                        "fastq_1": str(f1),
                        "fastq_2": str(tmp_path / "missing.fastq.gz"),
                    }
                ]
            )
        with pytest.raises(FileNotFoundError, match="empty-sample.*<empty>"):
            validate_sample_inputs([{"id": "empty-sample", "fastq_1": "", "fastq_2": str(f2)}])

    def test_no_sign_request_uses_unsigned_s3_client(self):
        mock_s3 = MagicMock()
        samples = [
            {
                "id": "pub",
                "fastq_1": "s3://pub-bucket/a_1.fastq.gz",
                "fastq_2": "s3://pub-bucket/a_2.fastq.gz",
            }
        ]
        with patch("submit_batch_jobs.boto3.client", return_value=mock_s3) as mock_client:
            validate_sample_inputs(samples, no_sign_request=True)
        mock_client.assert_called_once()
        assert mock_client.call_args.args == ("s3",)
        assert mock_client.call_args.kwargs["config"].signature_version is UNSIGNED

    def test_submit_batch_job_emits_preflight_before_process_substitution(self):
        mock_batch = MagicMock()
        mock_batch.submit_job.return_value = {"jobId": "job-999"}
        sample = {
            "id": "s1",
            "fastq_1": "s3://bucket/raw/s1_1.fastq.gz",
            "fastq_2": "s3://bucket/raw/s1_2.fastq.gz",
            "outdir": "s3://bucket/siz/",
        }
        submit_batch_job(
            mock_batch, sample, "queue", "job-def", 1000000, 15, no_sign_request=True
        )
        cmd = mock_batch.submit_job.call_args[1]["containerOverrides"]["command"][2]
        assert "aws s3 ls --no-sign-request s3://bucket/raw/s1_1.fastq.gz > /dev/null" in cmd
        assert "aws s3 ls --no-sign-request s3://bucket/raw/s1_2.fastq.gz > /dev/null" in cmd
        assert cmd.index("aws s3 ls") < cmd.index("sizer.sh")

    def test_main_exits_nonzero_before_submitting_when_samplesheet_file_missing(
        self, tmp_path: Path, capsys
    ):
        sheet = tmp_path / "samples.csv"
        sheet.write_text(
            "id,fastq_1,fastq_2,outdir\n"
            "bad,s3://does-not-exist/r1.fastq.gz,s3://does-not-exist/r2.fastq.gz,s3://out/\n",
            encoding="utf-8",
        )
        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )
        mock_batch = MagicMock()

        def fake_client(service, **kwargs):
            return mock_s3 if service == "s3" else mock_batch

        argv = [
            "submit_batch_jobs.py",
            "--sample-sheet",
            str(sheet),
            "--job-queue",
            "q",
            "--job-definition",
            "d",
        ]
        with patch.object(sys, "argv", argv), patch(
            "submit_batch_jobs.boto3.client", side_effect=fake_client
        ):
            with pytest.raises(SystemExit) as exc_info:
                submit_batch_jobs.main()

        assert exc_info.value.code == 1
        mock_batch.submit_job.assert_not_called()
        assert "s3://does-not-exist/r1.fastq.gz" in capsys.readouterr().err

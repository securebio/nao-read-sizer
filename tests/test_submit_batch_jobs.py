"""Simple tests for submit_batch_jobs.py"""
import pytest
from unittest.mock import MagicMock, patch
from submit_batch_jobs import submit_batch_job, monitor_jobs


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

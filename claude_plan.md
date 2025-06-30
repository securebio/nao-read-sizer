# Implementation Plan: Add --ignore-existing Flag to read-sizer

## Issue Description

### Problem Statement
Presently, when building a sample sheet we assume that any pair of fwd+rev FASTQ files that corresponds to one or more SIZ chunk files has been successfully processed, and we don't need to reprocess it: [source](https://github.com/naobservatory/read-sizer/blob/main/scripts/generate_samplesheet.py).

This is true in the case where someone has previously SIZed some of the FASTQ fwd+rev pairs in a delivery but not all; after that, we really don't need to reprocess FASTQ pairs that have corresponding SIZ files.

But if a previous run has failed partway through (and depending on what failure happened / how it was handled), a FASTQ pair can correspond to incomplete or corrupted SIZ files, in which case we do need to regenerate SIZ chunks for this FASTQ pair.

### Long-term Solution (Not in Current Scope)
The really nice feature here would be some validation mode which checks each FASTQ pair:
- Compute the number of read pairs in the FASTQ pair
- Find all matching SIZ chunks
- Validate that all matching SIZ chunks are well formatted (i.e. parse as FASTQ end to end)
- Validate that the collected SIZ chunks have the same total number of read pairs as the FASTQ pair
- (Possibly) Re-run SIZing for this FASTQ pair if it fails validation

### Immediate Need
In the meantime, a way to force `generate_samplesheet.py` to not skip seemingly-completed FASTQ pairs would allow us to re-run entire deliveries safely.

## Proposed Solution

Add an `--ignore-existing` flag to `generate_samplesheet.py` which will ignore any existing SIZ chunks, thus allowing for easy regeneration of entire delivery SIZes.

### Why This Approach?
- Simple and clear implementation
- Addresses immediate need to force regeneration when corruption is suspected
- Maintains backward compatibility (default behavior unchanged)
- Provides a pragmatic interim solution while the more sophisticated validation approach can be developed later

### Considerations
- This is a "sledgehammer" solution that will regenerate everything, even properly SIZed files
- Users need to be aware this will overwrite existing SIZ files
- Acceptable as an interim fix given the immediate need

## Step-by-Step Implementation Plan

### 1. Update `scripts/generate_samplesheet.py`

**File**: `scripts/generate_samplesheet.py`

**Changes**:
1. Add the command-line argument:
```python
parser.add_argument(
    "--ignore-existing",
    action="store_true",
    help="Ignore existing SIZ files and regenerate sample sheet for all FASTQ pairs"
)
```

2. Modify the logic that builds `processed_ids`:
```python
# Build processed IDs set
if args.ignore_existing:
    processed_ids = set()
else:
    # Existing logic
    siz_files = list_s3_files(siz_dir, allow_missing=True, no_sign_request=args.no_sign_request)
    processed_ids = set()
    for f in siz_files:
        if "_chunk" in f:
            id = f.partition("_chunk")[0]
            processed_ids.add(id)
```

3. Optionally add a print statement to indicate when ignore-existing mode is active:
```python
if args.ignore_existing:
    print("Ignoring existing SIZ files - all FASTQ pairs will be included in sample sheet")
```

### 2. Update Nextflow Process Definition

**File**: `modules/local/gen_samplesheet.nf`

**Changes**:
1. Add the new input parameter to the process:
```nextflow
process GENERATE_SAMPLESHEET {
    tag "${params.bucket}/${params.delivery}"
  
    input:
        val bucket
        val delivery
        val outdir
        val ignore_existing  // NEW: Add this parameter
        path script
  
    output:
        path "sample_sheet.csv"
  
    script:
        def outdir_param = outdir ? "--outdir ${outdir}" : ''
        def ignore_existing_param = ignore_existing ? "--ignore-existing" : ''  // NEW: Add this line
        """
        python3 ${script} --bucket ${bucket} --delivery ${delivery} ${outdir_param} ${ignore_existing_param} --output sample_sheet.csv
        """
}
```

### 3. Update Main Workflow

**File**: `main.nf`

**Changes**:
1. Update the GENERATE_SAMPLESHEET invocation to pass the new parameter:
```nextflow
sampleSheetChannel = GENERATE_SAMPLESHEET(
    params.bucket, 
    params.delivery,
    params.outdir ? params.outdir : '',
    params.ignore_existing,  // NEW: Add this line
    script
)
```

### 4. Update Nextflow Configuration

**File**: `nextflow.config`

**Changes**:
1. Add the new parameter with a default value:
```nextflow
params {
    // input-output config
    sample_sheet = null
    bucket = null
    delivery = null
    outdir = null
    ignore_existing = false  // NEW: Add this parameter with default false
    
    // SIZ options
    read_pairs_per_siz = 1000000
    zstd_level = 15
}
```

### 5. Update Test Files

**File**: `tests/modules/local/gen_samplesheet.nf.test`

**Changes**:
1. Update ALL existing test invocations of GENERATE_SAMPLESHEET to include the new parameter:

```nextflow
// Update the existing test "Should generate sample sheet from S3 bucket"
when {
    process {
        """
        input[0] = "nao-testing"
        input[1] = "read-sizer"
        input[2] = ""
        input[3] = false  // NEW: Add ignore_existing = false
        input[4] = file("${projectDir}/scripts/generate_samplesheet.py")
        """
    }
}

// Update the existing test "Should generate sample sheet with custom output directory"
when {
    process {
        """
        input[0] = "nao-testing"
        input[1] = "read-sizer"
        input[2] = "s3://nao-testing/read-sizer/custom"
        input[3] = false  // NEW: Add ignore_existing = false
        input[4] = file("${projectDir}/scripts/generate_samplesheet.py")
        """
    }
}

// Update the existing test "Should fail with invalid bucket"
when {
    process {
        """
        input[0] = "nonexistent-bucket-name-123456789"
        input[1] = "read-sizer"
        input[2] = ""
        input[3] = false  // NEW: Add ignore_existing = false
        input[4] = file("${projectDir}/scripts/generate_samplesheet.py")
        """
    }
}
```

2. Add a new test specifically for the ignore-existing functionality:
```nextflow
test("Should ignore existing SIZ files when flag is set") {
    when {
        process {
            """
            input[0] = "nao-testing"
            input[1] = "read-sizer"
            input[2] = ""
            input[3] = true  // ignore_existing = true
            input[4] = file("${projectDir}/scripts/generate_samplesheet.py")
            """
        }
    }
    
    then {
        assert process.success
        
        // Get the sample sheet content
        def sampleSheetFile = process.out.get(0).toString()
        def cleanPath = sampleSheetFile.replaceAll(/^\[|\]$/, '')
        def sampleSheetContent = path(cleanPath).text
        def lines = sampleSheetContent.readLines()
        
        // When ignore_existing is true, the sample sheet should include
        // all FASTQ pairs, even those that might have existing SIZ files
        assert lines.size() >= 1  // At least header
        
        // Additional assertions could be added here if we have
        // test data with known existing SIZ files
    }
}
```

### 6. Update Documentation

**File**: `README.md`

**Changes**:
1. Add a new section after the existing "Automatically generated sample sheet" content:

```markdown
#### Forcing regeneration of existing SIZ files

By default, the automatic sample sheet generation skips FASTQ pairs that already have corresponding SIZ files in the output directory. This prevents unnecessary reprocessing of already-SIZed data.

However, if you suspect existing SIZ files are corrupted or incomplete (e.g., from a previous failed run), you can force regeneration of all SIZ files using the `--ignore-existing` flag:

```bash
nextflow run main.nf --bucket my-data-bucket --delivery delivery-to-siz --ignore-existing
```

**Warning**: This will regenerate SIZ files for ALL FASTQ pairs in the delivery, overwriting any existing SIZ files in the output directory. Use with caution.

**Note**: This is an interim solution. Future versions may include validation of existing SIZ files to automatically detect and reprocess only corrupted or incomplete files.
```

2. Consider adding `--ignore-existing` to any command-line examples in the README where it might be relevant.

## Testing Plan

### Manual Testing
1. Run the pipeline on a delivery that has no existing SIZ files - verify normal behavior
2. Run the pipeline on a delivery with some existing SIZ files - verify it skips those pairs
3. Run the pipeline with `--ignore-existing` on a delivery with existing SIZ files - verify it processes all pairs
4. Test the script directly: `python3 scripts/generate_samplesheet.py --bucket X --delivery Y --ignore-existing`

### Automated Testing
1. Run the updated nf-test suite: `nf-test test`
2. Verify all existing tests still pass with the new parameter structure
3. Verify the new test for `--ignore-existing` functionality passes

### Integration Testing
1. Test with the standard profile
2. Test with the batch profile
3. Test with batch,high_perf profiles
4. Verify the parameter is correctly passed through all workflow stages

## Implementation Order

1. **Start with the Python script** (`generate_samplesheet.py`) - this is the core change
2. **Update the Nextflow configuration** to add the parameter
3. **Update the Nextflow process and workflow** to pass the parameter
4. **Update all tests** to match the new parameter structure
5. **Update documentation**
6. **Run complete test suite** to ensure nothing is broken

## Rollback Plan

If issues are discovered:
1. The change is backward compatible (default is `false`), so existing workflows will continue to work
2. To rollback, simply don't use the `--ignore-existing` flag
3. If critical issues found, revert the commit(s) implementing this feature

## Future Enhancements

After this interim solution is implemented, consider developing the full validation approach:
1. Add a `--validate` mode that checks SIZ file integrity
2. Implement read counting for both FASTQ and SIZ files
3. Add automatic detection and regeneration of only corrupted files
4. Consider adding checksums or other integrity markers to SIZ files

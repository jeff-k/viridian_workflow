"""The pipeline definition
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Any

# import tempfile
import json

from viridian_workflow import readstore, self_qc
from viridian_workflow.subtasks import Cylon, Minimap, Varifier
from viridian_workflow.primers import AmpliconSet


def run_pipeline(
    work_dir: Path,
    platform: str,
    fqs: list[Path],
    amplicon_sets: list[AmpliconSet],
    ref: Path = Path("../covid/MN908947.fasta"),
    force_amp_scheme: Optional[AmpliconSet] = None,
    keep_intermediate: bool = False,
    keep_bam: bool = False,
    sample_name: str = "sample",
    frs_threshold: float = 0.1,
    self_qc_depth: int = 20,
    consensus_max_n_percent: int = 50,
    max_percent_amps_fail: int = 50,
    dump_tsv: bool = False,
    command_line_args: Optional[dict[str, Any]] = None,
    force_consensus: Optional[Path] = None,
):

    log: dict[str, Any] = {}
    work_dir = Path(work_dir)
    if work_dir.exists():
        raise Exception(f"Output directory {work_dir} already exists")
    work_dir.mkdir()

    # generate name-sorted bam from fastqs
    if platform == "illumina":
        fq1, fq2 = fqs
        minimap = Minimap(work_dir / "name_sorted.bam", ref, fq1, fq2=fq2, sort=False)
    elif platform == "ont":
        fq = fqs[0]
        minimap = Minimap(work_dir / "name_sorted.bam", ref, fq, sort=False)
    elif platform == "iontorrent":
        raise NotImplementedError
    else:
        print(f"Platform {platform} is not supported.", file=sys.stderr)
        exit(1)

    unsorted_bam: Path = minimap.run()
    # add minimap task log to result log
    # log["minimap"] = minimap.log

    # pre-process input bam
    bam: readstore.Bam = readstore.Bam(unsorted_bam)

    # detect amplicon set
    amplicon_set: AmpliconSet = bam.detect_amplicon_set(amplicon_sets)
    # log["amplicons"] = bam.stats

    # construct readstore
    # this subsamples the reads
    reads = (
        readstore.ReadStore(amplicon_set, bam)
        if force_amp_scheme is None
        else readstore.ReadStore(force_amp_scheme, bam)
    )

    log["amplicons"] = reads.summary

    # branch on whether to run cylon or use external assembly ("cuckoo mode")
    consensus: Optional[Path] = None
    if force_consensus is not None:
        log["forced_consensus"] = str(force_consensus)
        consensus = Path(force_consensus)

    else:
        # save reads for cylon assembly
        amp_dir = work_dir / "amplicons"
        manifest_data = reads.make_reads_dir_for_cylon(amp_dir)

        # run cylon
        cylon = Cylon(work_dir, platform, ref, amp_dir, manifest_data, reads.cylon_json)
        consensus = cylon.run()
        log["initial_assembly"] = cylon.log

    # satify type bounds and ensure the readstore was properly constructed
    assert consensus is not None
    assert reads.start_pos is not None
    assert reads.end_pos is not None

    # varifier
    varifier = Varifier(
        work_dir / "varifier",
        ref,
        consensus,
        min_coord=reads.start_pos,
        max_coord=reads.end_pos,
    )
    vcf, msa, varifier_consensus = varifier.run()
    log["varifier"] = varifier.log

    pileup = self_qc.Pileup(
        varifier_consensus,
        reads,
        msa=msa,
        config=self_qc.Config(frs_threshold, self_qc_depth),
    )

    # masked fasta output
    masked_fasta: str = pileup.mask()
    # log["self_qc"] = pileup.log
    log["qc"] = pileup.summary

    print(json.dumps(log), file=open(work_dir / "log.json", "w"))

    with open(work_dir / "consensus.fa", "w", encoding="utf-8") as fasta_out:
        print(f">{sample_name}", file=fasta_out)
        print(masked_fasta, file=fasta_out)

    # annotate vcf
    annotated_vcf = pileup.annotate_vcf(vcf)

    # dump tsv
    if dump_tsv:
        _ = pileup.dump_tsv(work_dir / "all_stats.tsv")

    with open(work_dir / "final.vcf", "w", encoding="utf-8") as vcf_out:
        header, records = annotated_vcf
        for h in header:
            print(h, file=vcf_out)
        for rec in records:
            print("\t".join(map(str, rec)), file=vcf_out)

    return log

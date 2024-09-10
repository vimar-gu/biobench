"""
Entrypoint for running all benchmarks.

.. include:: ./tutorial.md
"""

import concurrent.futures
import dataclasses
import json
import logging
import multiprocessing
import os
import time
import typing

import beartype
import torch
import tyro

from biobench import (
    interfaces,
    iwildcam,
    kabr,
    load_vision_backbone,
    newt,
    plantnet,
    registry,
)

log_format = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=log_format)
logger = logging.getLogger("biobench")

if typing.TYPE_CHECKING:
    # Static type seen by language servers, type checkers, etc.
    ModelOrg = str
else:
    # Runtime type used by tyro.
    ModelOrg = tyro.extras.literal_type_from_choices(registry.list_vision_backbones())


@beartype.beartype
@dataclasses.dataclass(frozen=True)
class Args:
    """Params to run one or more benchmarks in a parallel setting."""

    jobs: typing.Literal["slurm", "process", "none"] = "none"
    """what kind of jobs we should use for parallel processing: slurm cluster, multiple processes on the same machine, or no parallelism (a single process)."""

    # How to set up the model.
    model_org: ModelOrg = "open_clip"
    """Where to load models from."""
    model_ckpt: str = "RN50/openai"
    device: typing.Literal["cpu", "cuda"] = "cuda"
    """which kind of accelerator to use."""

    # Individual benchmarks.
    newt_run: bool = False
    """whether to run the NeWT benchmark."""
    newt_args: newt.Args = dataclasses.field(default_factory=newt.Args)
    """arguments for the NeWT benchmark."""
    kabr_run: bool = False
    """whether to run the KABR benchmark."""
    kabr_args: kabr.Args = dataclasses.field(default_factory=kabr.Args)
    """arguments for the KABR benchmark."""
    plantnet_run: bool = False
    """whether to run the Pl@ntNet benchmark."""
    plantnet_args: plantnet.Args = dataclasses.field(default_factory=plantnet.Args)
    """arguments for the Pl@ntNet benchmark."""
    iwildcam_run: bool = False
    """whether to run the iWildCam benchmark."""
    iwildcam_args: iwildcam.Args = dataclasses.field(default_factory=iwildcam.Args)
    """arguments for the iWildCam benchmark."""

    # Saving
    report_to: str = os.path.join(".", "reports")
    """where to save reports to."""

    def report_path(self, report: interfaces.BenchmarkReport) -> str:
        posix = int(time.time())
        return os.path.join(args.report_to, f"{posix}.jsonl")

    def to_dict(self):
        return dataclasses.asdict(self)


class DummyExecutor(concurrent.futures.Executor):
    """Dummy class to satisfy the Executor interface. Directly runs the function in the main process for easy debugging."""

    def submit(self, fn, /, *args, **kwargs):
        """runs `fn` directly in the main process and returns a `concurrent.futures.Future` with the result.

        Returns:
        """
        future = concurrent.futures.Future()
        try:
            result = fn(*args, **kwargs)
            future.set_result(result)
        except Exception as exc:
            future.set_exception(exc)

        return future


@beartype.beartype
def save(args: Args, report: interfaces.BenchmarkReport) -> None:
    """
    Saves the report to disk in a machine-readable JSON format.
    """
    report_dct = report.to_dict()
    report_dct["run_args"] = dataclasses.asdict(args)

    report_dct["mean_score"] = report.get_mean_score()
    lower, upper = report.get_confidence_interval()
    report_dct["confidence_interval_lower"] = lower
    report_dct["confidence_interval_upper"] = upper

    with open(args.report_path(report), "a") as fd:
        fd.write(json.dumps(report_dct) + "\n")

    logger.info(
        "%s on %s: %.1f%%", args.model_ckpt, report.name, report.get_mean_score() * 100
    )
    for key, value in report.splits.items():
        logger.info(
            "%s on %s; split '%s': %.3f", args.model_ckpt, report.name, key, value
        )


@beartype.beartype
def main(args: Args):
    if args.jobs == "process":
        executor = concurrent.futures.ProcessPoolExecutor()
    elif args.jobs == "slurm":
        raise NotImplementedError("submitit not tested yet!")
        # TODO
        # executor = submitit.AutoExecutor()
    elif args.jobs == "none":
        executor = DummyExecutor()
    else:
        typing.assert_never(args.jobs)

    # 1. Load model.
    backbone = load_vision_backbone(args.model_org, args.model_ckpt)

    # 2. Run benchmarks.
    jobs = []
    if args.newt_run:
        newt_args = dataclasses.replace(args.newt_args, device=args.device)
        jobs.append(executor.submit(newt.benchmark, backbone, newt_args))
    if args.kabr_run:
        kabr_args = dataclasses.replace(args.kabr_args, device=args.device)
        jobs.append(executor.submit(kabr.benchmark, backbone, kabr_args))
    if args.plantnet_run:
        plantnet_args = dataclasses.replace(args.plantnet_args, device=args.device)
        jobs.append(executor.submit(plantnet.benchmark, backbone, plantnet_args))
    if args.iwildcam_run:
        iwildcam_args = dataclasses.replace(args.iwildcam_args, device=args.device)
        jobs.append(executor.submit(iwildcam.benchmark, backbone, iwildcam_args))

    # 3. Display results.
    os.makedirs(args.report_to, exist_ok=True)
    for future in concurrent.futures.as_completed(jobs):
        if future.exception():
            logger.warning("Error running job: %s", future.exception())
            continue

        report = future.result()
        save(args, report)


if __name__ == "__main__":
    args = tyro.cli(Args)

    # 0. Check on hardware accelerator.
    if args.device == "cuda" and not torch.cuda.is_available():
        logger.warning("No CUDA GPU found. Using CPU instead.")
        # Can't use CUDA, so might be on macOS, which cannot use spawn with pickle.
        multiprocessing.set_start_method("fork")
        args = dataclasses.replace(args, device="cpu")
    elif args.device == "cuda" and torch.cuda.is_available():
        multiprocessing.set_start_method("spawn")

    main(args)

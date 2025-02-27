import json
from pathlib import Path
from textwrap import dedent
import docker

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Scorer, scorer, accuracy, stderr, mean, std
from inspect_ai.solver import Solver, TaskState, basic_agent, system_message
from inspect_ai.tool import bash
from inspect_ai.util import SandboxEnvironmentSpec, sandbox

from mlebench.data import download_and_prepare_dataset, is_dataset_prepared
from mlebench.registry import registry


DATA_DIR = (Path.cwd() / "data").resolve()
COMPOSE_FILES_DIR = (Path.cwd() / "cache" / "compose_files").resolve()
IMAGE_NAME = "mlebench-inspect-env"

with open("instructions.txt", "r") as f:
    INSTRUCTIONS = f.read()


@task
def mle_bench(split="spaceship-titanic.txt") -> Task:
    with open(Path("splits") / split) as f:
        competition_ids = f.read().splitlines()

    new_registry = registry.set_data_dir(DATA_DIR)
    samples = []

    for competition_id in competition_ids:
        competition = new_registry.get_competition(competition_id)
        if not is_dataset_prepared(competition):
            print(f"Preparing competition {competition_id}")
            download_and_prepare_dataset(competition)

        samples.append(
            Sample(
                input=INSTRUCTIONS,
                id=competition_id,
                sandbox=SandboxEnvironmentSpec(
                    type="docker",
                    config=get_compose_file(competition_id),
                ),
            )
        )

    return Task(
        dataset=MemoryDataset(samples=samples),
        solver=default_solver(),
        scorer=mle_bench_scorer(),
    )


def default_solver(max_messages: int = 30) -> Solver:
    return basic_agent(
        init=[
            system_message(
                "Please solve the coding task below. Once you are done, use your submit tool."
            )
        ],
        tools=[bash(timeout=180, user="nonroot")],
        max_messages=max_messages,
    )


@scorer(
    metrics={
        "valid_submission": [accuracy(), stderr()],
        "above_median": [accuracy(), stderr()],
        "any_medal": [accuracy(), stderr()],
        "score": [mean(), std()],
    }
)
def mle_bench_scorer() -> Scorer:
    async def scorer(state: TaskState, target) -> Score:
        grading_script = dedent(f"""
            from pathlib import Path
            import json
            from mlebench.grade import grade_csv
            from mlebench.registry import registry
            
            competition = registry.set_data_dir(Path("/private/data")).get_competition("{state.sample_id}")
            report = grade_csv(Path("/home/submission/submission.csv"), competition)
            print(json.dumps(report.to_dict())) 
        """)
        result = await sandbox().exec(["python", "-c", grading_script])

        try:
            report_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            print("Failed to parse grading report")
            report_data = {}

        return Score(
            value={
                "valid_submission": report_data.get("valid_submission", False),
                "above_median": report_data.get("above_median", False),
                "any_medal": report_data.get("any_medal", False),
                "score": report_data.get("score", None)
            },
            explanation=result.stdout
        )   

    return scorer



def get_compose_file(competition_id: str, force_rebuild: bool = False) -> str:
    image_compose_file = COMPOSE_FILES_DIR / f"{competition_id}.yaml"
    image_compose_file.parent.mkdir(parents=True, exist_ok=True)

    with image_compose_file.open(mode="w+") as f:
        f.write(dedent(f"""\
            services:
              default:
                image: {IMAGE_NAME}
                platform: linux/amd64
                network_mode: none
                volumes:
                  - {DATA_DIR / competition_id / "prepared/public"}:/home/data:ro
                  - {DATA_DIR / competition_id / "prepared/private"}:/private/data/{competition_id}/prepared/private:ro
                environment:
                  - COMPETITION_ID={competition_id}
                x-local: true
                deploy:
                  resources:
                    limits:
                      cpus: '1'
        """))


        try:
            docker.from_env().images.get(IMAGE_NAME)
            image_exists = True
        except docker.errors.ImageNotFound:
            image_exists = False

        if force_rebuild or not image_exists:
            f.write("    build: ../../")

    return str(image_compose_file)

import asyncio
import asyncio.subprocess
import os
import shutil
import shlex
from pathlib import Path

from pydantic import BaseModel

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths


class DockerEnvironmentEnvVars(BaseModel):
    main_image_name: str
    context_dir: str
    test_dir: str
    host_verifier_logs_path: str
    host_agent_logs_path: str
    env_verifier_logs_path: str
    env_agent_logs_path: str
    prebuilt_image_name: str | None = None
    cpus: int = 1
    memory: str = "1G"
    network_mode: str = "bridge"

    def to_env_dict(self, include_os_env: bool = True) -> dict[str, str]:
        env_dict = {} if not include_os_env else os.environ.copy()

        for field_name, value in self.model_dump(exclude_none=True).items():
            if value is None:
                continue

            env_dict[f"{field_name.upper()}"] = str(value)

        return env_dict


class DockerEnvironment(BaseEnvironment):
    _DOCKER_COMPOSE_BUILD_PATH = Path(__file__).parent / "docker-compose-build.yaml"
    _DOCKER_COMPOSE_PREBUILT_PATH = (
        Path(__file__).parent / "docker-compose-prebuilt.yaml"
    )

    # Class-level lock per image name to prevent parallel builds of the same image.
    _image_build_locks: dict[str, asyncio.Lock] = {}
    
    # Cached container runtime detection
    _container_runtime: str | None = None
    
    @classmethod
    def _get_container_runtime(cls) -> str:
        """
        Detect which container runtime to use: 'podman' or 'docker'.
        Checks HARBOR_CONTAINER_RUNTIME env var first, then auto-detects.
        """
        if cls._container_runtime is not None:
            return cls._container_runtime
        
        # Check environment variable first
        runtime = os.environ.get("HARBOR_CONTAINER_RUNTIME", "").lower()
        if runtime in ("podman", "docker"):
            cls._container_runtime = runtime
            return runtime
        
        # Auto-detect: prefer podman if available and HARBOR_USE_PODMAN is set
        if os.environ.get("HARBOR_USE_PODMAN", "").lower() in ("1", "true", "yes"):
            if shutil.which("podman"):
                cls._container_runtime = "podman"
                return "podman"
        
        # Default to docker
        cls._container_runtime = "docker"
        return "docker"
    
    @classmethod
    def _get_compose_command(cls) -> list[str]:
        """
        Get the compose command to use. For Podman, we need to use 'podman-compose'
        directly to avoid the wrapper loop, or set PODMAN_COMPOSE_PROVIDER.
        """
        runtime = cls._get_container_runtime()
        if runtime == "podman":
            # Check if PODMAN_COMPOSE_PROVIDER is set
            provider = os.environ.get("PODMAN_COMPOSE_PROVIDER")
            if provider:
                return [provider]
            # Try to use podman-compose directly if available
            if shutil.which("podman-compose"):
                return ["podman-compose"]
            # Otherwise use podman compose (which will delegate)
            return ["podman", "compose"]
        else:
            return ["docker", "compose"]

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        keep_containers: bool = False,
        *args,
        **kwargs,
    ):
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            **kwargs,
        )

        self._keep_containers = keep_containers

        self._env_vars = DockerEnvironmentEnvVars(
            main_image_name=f"hb__{environment_name}",
            context_dir=str(self.environment_dir.resolve().absolute()),
            test_dir=str(EnvironmentPaths.tests_dir),
            host_verifier_logs_path=str(trial_paths.verifier_dir.resolve().absolute()),
            host_agent_logs_path=str(trial_paths.agent_dir.resolve().absolute()),
            env_verifier_logs_path=str(EnvironmentPaths.verifier_dir),
            env_agent_logs_path=str(EnvironmentPaths.agent_dir),
            prebuilt_image_name=task_env_config.docker_image,
            cpus=task_env_config.cpus,
            memory=f"{task_env_config.memory_mb}M",
            network_mode="bridge" if task_env_config.allow_internet else "none",
        )
        self._use_prebuilt = False

    @staticmethod
    def type() -> EnvironmentType:
        return EnvironmentType.DOCKER

    @property
    def supports_gpus(self) -> bool:
        return False

    @property
    def can_disable_internet(self) -> bool:
        return True

    @property
    def is_mounted(self) -> bool:
        return True

    @property
    def _dockerfile_path(self) -> Path:
        return self.environment_dir / "Dockerfile"

    @property
    def _environment_docker_compose_path(self) -> Path:
        return self.environment_dir / "docker-compose.yaml"

    @property
    def _docker_compose_path(self) -> Path:
        """
        Returns the appropriate docker-compose file based on whether we're using a
        prebuilt image.
        """
        if self._environment_docker_compose_path.exists():
            return self._environment_docker_compose_path

        if self._use_prebuilt:
            return self._DOCKER_COMPOSE_PREBUILT_PATH

        return self._DOCKER_COMPOSE_BUILD_PATH

    def _validate_definition(self):
        if (
            not self._dockerfile_path.exists()
            and not self._environment_docker_compose_path.exists()
        ):
            raise FileNotFoundError(
                f"{self._dockerfile_path} and {self._environment_docker_compose_path} "
                "not found. Please ensure at least one of these files exist."
            )

    async def _run_docker_compose_command(
        self, command: list[str], check: bool = True, timeout_sec: int | None = None
    ) -> ExecResult:
        """Run a docker compose command and return the result."""
        compose_cmd = self._get_compose_command()
        full_command = [
            *compose_cmd,
            "-p",
            self.session_id.lower().replace(".", "-"),
            "-f",
            str(self._docker_compose_path.resolve().absolute()),
            *command,
        ]

        process = await asyncio.create_subprocess_exec(
            *full_command,
            env=self._env_vars.to_env_dict(include_os_env=True),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            if timeout_sec:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_sec
                )
            else:
                stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.TimeoutError:
            process.terminate()
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=5
                )
            except asyncio.TimeoutError:
                process.kill()
                stdout_bytes, stderr_bytes = await process.communicate()
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds")

        stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else None
        stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else None

        result = ExecResult(
            stdout=stdout,
            stderr=stderr,
            return_code=process.returncode or 0,
        )

        if check and result.return_code != 0:
            runtime_name = self._get_container_runtime()
            raise RuntimeError(
                f"{runtime_name.capitalize()} compose command failed for environment {self.environment_name}. "
                f"Command: {' '.join(full_command)}. "
                f"Return code: {result.return_code}. "
                f"Stdout: {result.stdout}. "
                f"Stderr: {result.stderr}. "
            )

        return result

    async def start(self, force_build: bool):
        self._use_prebuilt = (
            not force_build and self.task_env_config.docker_image
        )

        if not self._use_prebuilt:
            # Serialize image builds: if multiple environments with the same image name
            # start concurrently, only one builds while others wait for the cached image.
            image_name = self._env_vars.main_image_name
            lock = self._image_build_locks.setdefault(image_name, asyncio.Lock())
            async with lock:
                await self._run_docker_compose_command(["build"])

        # up -d can run concurrently
        await self._run_docker_compose_command(["up", "-d"])

    async def stop(self, delete: bool):
        if self._keep_containers and delete:
            self.logger.warning(
                "Both `keep_containers` and `--delete` option are set. "
                "keep_containers takes precedence."
            )
        if self._keep_containers:
            try:
                await self._run_docker_compose_command(["stop"])
            except RuntimeError as e:
                self.logger.warning(f"Docker compose stop failed: {e}")
        elif delete:
            try:
                await self._run_docker_compose_command(
                    ["down", "--rmi", "all", "--volumes", "--remove-orphans"]
                )
            except RuntimeError as e:
                self.logger.warning(f"Docker compose down failed: {e}")

            # await self._cleanup_build_cache()
        else:
            try:
                await self._run_docker_compose_command(["down"])
            except RuntimeError as e:
                self.logger.warning(f"Docker compose down failed: {e}")

    async def _get_container_name(self) -> str:
        """Get the container name for the main service."""
        runtime = self._get_container_runtime()
        project_name = self.session_id.lower().replace(".", "-")
        
        if runtime == "podman":
            # For podman-compose, container name is typically {project_name}_main_1
            # But we should query it to be sure
            compose_cmd = self._get_compose_command()
            ps_command = [
                *compose_cmd,
                "-p",
                project_name,
                "-f",
                str(self._docker_compose_path.resolve().absolute()),
                "ps",
                "-q",
            ]
            
            process = await asyncio.create_subprocess_exec(
                *ps_command,
                env=self._env_vars.to_env_dict(include_os_env=True),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await process.communicate()
            container_id = stdout_bytes.decode().strip()
            
            if container_id:
                return container_id
            # Fallback to expected name pattern
            return f"{project_name}_main_1"
        else:
            # For docker compose, use the service name format
            return f"{project_name}-main-1"

    async def upload_file(self, source_path: Path | str, target_path: str):
        runtime = self._get_container_runtime()
        compose_cmd = self._get_compose_command()
        
        # podman-compose doesn't support cp, use podman cp directly
        if runtime == "podman" and compose_cmd == ["podman-compose"]:
            container_name = await self._get_container_name()
            process = await asyncio.create_subprocess_exec(
                "podman",
                "cp",
                str(source_path),
                f"{container_name}:{target_path}",
                env=self._env_vars.to_env_dict(include_os_env=True),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else None
            stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else None
            return_code = process.returncode or 0
            
            if return_code != 0:
                raise RuntimeError(
                    f"Podman cp command failed. "
                    f"Command: podman cp {source_path} {container_name}:{target_path}. "
                    f"Return code: {return_code}. "
                    f"Stdout: {stdout}. "
                    f"Stderr: {stderr}. "
                )
        else:
            await self._run_docker_compose_command(
                [
                    "cp",
                    str(source_path),
                    f"main:{target_path}",
                ],
                check=True,
            )

    async def upload_dir(self, source_dir: Path | str, target_dir: str):
        runtime = self._get_container_runtime()
        compose_cmd = self._get_compose_command()
        
        # podman-compose doesn't support cp, use podman cp directly
        if runtime == "podman" and compose_cmd == ["podman-compose"]:
            container_name = await self._get_container_name()
            process = await asyncio.create_subprocess_exec(
                "podman",
                "cp",
                str(source_dir),
                f"{container_name}:{target_dir}",
                env=self._env_vars.to_env_dict(include_os_env=True),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else None
            stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else None
            return_code = process.returncode or 0
            
            if return_code != 0:
                raise RuntimeError(
                    f"Podman cp command failed. "
                    f"Command: podman cp {source_dir} {container_name}:{target_dir}. "
                    f"Return code: {return_code}. "
                    f"Stdout: {stdout}. "
                    f"Stderr: {stderr}. "
                )
        else:
            await self._run_docker_compose_command(
                [
                    "cp",
                    str(source_dir),
                    f"main:{target_dir}",
                ],
                check=True,
            )

    async def download_file(self, source_path: str, target_path: Path | str):
        runtime = self._get_container_runtime()
        compose_cmd = self._get_compose_command()
        
        # podman-compose doesn't support cp, use podman cp directly
        if runtime == "podman" and compose_cmd == ["podman-compose"]:
            container_name = await self._get_container_name()
            process = await asyncio.create_subprocess_exec(
                "podman",
                "cp",
                f"{container_name}:{source_path}",
                str(target_path),
                env=self._env_vars.to_env_dict(include_os_env=True),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else None
            stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else None
            return_code = process.returncode or 0
            
            if return_code != 0:
                raise RuntimeError(
                    f"Podman cp command failed. "
                    f"Command: podman cp {container_name}:{source_path} {target_path}. "
                    f"Return code: {return_code}. "
                    f"Stdout: {stdout}. "
                    f"Stderr: {stderr}. "
                )
        else:
            await self._run_docker_compose_command(
                [
                    "cp",
                    f"main:{source_path}",
                    str(target_path),
                ],
                check=True,
            )

    async def download_dir(self, source_dir: str, target_dir: Path | str):
        runtime = self._get_container_runtime()
        compose_cmd = self._get_compose_command()
        
        # podman-compose doesn't support cp, use podman cp directly
        if runtime == "podman" and compose_cmd == ["podman-compose"]:
            container_name = await self._get_container_name()
            process = await asyncio.create_subprocess_exec(
                "podman",
                "cp",
                f"{container_name}:{source_dir}",
                str(target_dir),
                env=self._env_vars.to_env_dict(include_os_env=True),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else None
            stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else None
            return_code = process.returncode or 0
            
            if return_code != 0:
                raise RuntimeError(
                    f"Podman cp command failed. "
                    f"Command: podman cp {container_name}:{source_dir} {target_dir}. "
                    f"Return code: {return_code}. "
                    f"Stdout: {stdout}. "
                    f"Stderr: {stderr}. "
                )
        else:
            await self._run_docker_compose_command(
                [
                    "cp",
                    f"main:{source_dir}",
                    str(target_dir),
                ],
                check=True,
            )

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> ExecResult:
        runtime = self._get_container_runtime()
        compose_cmd = self._get_compose_command()
        
        # podman-compose exec doesn't support -it flags, use podman exec directly
        if runtime == "podman" and compose_cmd == ["podman-compose"]:
            container_name = await self._get_container_name()
            exec_command = ["podman", "exec", "-i"]
            
            if cwd:
                exec_command.extend(["-w", cwd])
            
            if env:
                for key, value in env.items():
                    exec_command.extend(["-e", f"{key}={shlex.quote(value)}"])
            
            exec_command.append(container_name)
            exec_command.extend(["bash", "-ic", command])
            
            process = await asyncio.create_subprocess_exec(
                *exec_command,
                env=self._env_vars.to_env_dict(include_os_env=True),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            
            try:
                if timeout_sec:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        process.communicate(), timeout=timeout_sec
                    )
                else:
                    stdout_bytes, stderr_bytes = await process.communicate()
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        process.communicate(), timeout=5
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    stdout_bytes, stderr_bytes = await process.communicate()
                raise RuntimeError(f"Command timed out after {timeout_sec} seconds")
            
            stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else None
            stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else None
            
            return ExecResult(
                stdout=stdout,
                stderr=stderr,
                return_code=process.returncode or 0,
            )
        else:
            # Docker compose exec supports -it
            exec_command = ["exec", "-it"]

            if cwd:
                exec_command.extend(["-w", cwd])

            if env:
                for key, value in env.items():
                    exec_command.extend(["-e", f"{key}={shlex.quote(value)}"])

            exec_command.append("main")
            exec_command.extend(["bash", "-ic", command])

            return await self._run_docker_compose_command(
                exec_command, check=False, timeout_sec=timeout_sec
            )

    async def _cleanup_build_cache(self) -> None:
        """Clean up container build cache to free up disk space."""
        runtime = self._get_container_runtime()
        try:
            if runtime == "podman":
                # Podman uses 'podman system prune' for cache cleanup
                process = await asyncio.create_subprocess_exec(
                    runtime,
                    "system",
                    "prune",
                    "--force",
                    "--volumes",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await process.wait()
            else:
                # Docker: Use --max-used-space to keep cache under the specified size limit
                try:
                    process = await asyncio.create_subprocess_exec(
                        runtime,
                        "buildx",
                        "prune",
                        "--force",
                        "--max-used-space",
                        "30GB",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await process.wait()
                except Exception:
                    # Fallback to docker builder prune if buildx is not available
                    try:
                        process = await asyncio.create_subprocess_exec(
                            runtime,
                            "builder",
                            "prune",
                            "--force",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await process.wait()
                    except Exception:
                        # Silent fail - build cache cleanup is not critical
                        pass
        except Exception:
            # Silent fail - build cache cleanup is not critical
            pass

    async def attach(self) -> None:
        variables = " ".join(
            f"export {k}={shlex.quote(str(v))}"
            for k, v in self._env_vars.to_env_dict(include_os_env=False).items()
        )
        
        compose_cmd = self._get_compose_command()
        compose_cmd.extend([
            "-p",
            self.session_id.lower().replace(".", "-"),
            "-f",
            str(self._docker_compose_path.resolve().absolute()),
        ])

        os.execvp(
            "bash",
            [
                "bash",
                "-c",
                f"{variables}; "
                + " ".join(
                    [
                        *compose_cmd,
                        "exec",
                        "-it",
                        "main",
                        "bash",
                        ";",
                        *compose_cmd,
                        "down",
                    ]
                ),
            ],
        )

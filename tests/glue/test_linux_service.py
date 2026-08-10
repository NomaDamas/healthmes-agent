from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_linux_service_is_persistent_and_preserves_data_on_uninstall() -> None:
    script = (ROOT / "scripts" / "healthmes_linux.sh").read_text(encoding="utf-8")
    unit = (
        ROOT / "config" / "systemd" / "healthmes-compose.service.in"
    ).read_text(encoding="utf-8")

    assert '"$SYSTEMCTL_BIN" --user enable --now' in script
    assert "loginctl enable-linger" in script
    assert "Docker volumes and personal data were kept" in script
    assert (
        "ExecStart=__DOCKER__ compose -f docker-compose.yml "
        "-f docker-compose.linux.yml up -d --build"
    ) in unit
    assert "Requires=docker.service" not in unit
    assert "ExecStartPre=__LINUX_SCRIPT__ wait-for-docker" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=10" in unit
    assert "WantedBy=default.target" in unit
    assert "DOCKER_READY_ATTEMPTS" in script
    assert "wait_for_docker" in script
    assert '"$DOCKER_BIN" compose "${COMPOSE_FILES[@]}" config' in script
    assert "systemd_quote" in script
    assert "sed_replacement" in script
    assert 'git -C "$REPO_ROOT" pull --ff-only' in script
    assert "bootstrap_runtime" in script


def test_compose_services_restart_after_host_reboot() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "restart: on-failure" not in compose
    assert compose.count("restart: unless-stopped") >= 8


def test_linux_compose_routes_hermes_to_host_network_healthmes() -> None:
    override = (ROOT / "docker-compose.linux.yml").read_text(encoding="utf-8")
    assert '"host.docker.internal:host-gateway"' in override
    assert "HEALTHMES_PORT=${HEALTHMES_PORT:-8100}" in override
    setup = (ROOT / "scripts" / "healthmes_setup.py").read_text(encoding="utf-8")
    assert 'f"http://host.docker.internal:{port}/mcp"' in setup

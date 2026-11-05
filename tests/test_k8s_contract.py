"""Contrato entre os manifestos do Kubernetes e o código: o que o YAML promete, a API cumpre."""

import re
from pathlib import Path

import yaml

from obs_api.main import PUBLIC_PATHS

K8S = Path(__file__).resolve().parent.parent / "deploy" / "k8s"


def load(name):
    return yaml.safe_load((K8S / name).read_text(encoding="utf-8"))


def container(doc):
    return doc["spec"]["template"]["spec"]["containers"][0]


def test_probes_point_to_public_routes():
    c = container(load("deployment.yaml"))
    assert c["livenessProbe"]["httpGet"]["path"] in PUBLIC_PATHS
    assert c["readinessProbe"]["httpGet"]["path"] in PUBLIC_PATHS


def test_configmap_only_sets_variables_the_code_reads():
    read = set(re.findall(r'"([A-Z][A-Z_]{3,})"', (K8S.parent.parent / "obs_api" / "config.py").read_text(encoding="utf-8")))
    unknown = set(load("configmap.yaml")["data"]) - read
    assert not unknown, f"variáveis no ConfigMap que o código ignora: {unknown}"


def test_pod_is_locked_down():
    dep = load("deployment.yaml")
    pod, c = dep["spec"]["template"]["spec"], container(dep)
    assert pod["securityContext"]["runAsNonRoot"] is True and pod["automountServiceAccountToken"] is False
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_single_replica_because_sqlite_on_rwo_volume():
    dep = load("deployment.yaml")
    assert dep["spec"]["replicas"] == 1 and dep["spec"]["strategy"]["type"] == "Recreate"
    assert load("pvc.yaml")["spec"]["accessModes"] == ["ReadWriteOnce"]


def test_purge_cronjob_runs_the_real_cli_command():
    args = container(load("cronjob-purge.yaml")["spec"]["jobTemplate"])["args"]
    assert args[-1] == "purge"


def test_example_secret_holds_only_a_placeholder_hash():
    assert "TROQUE_PELO_HASH" in load("secret.example.yaml")["stringData"]["API_KEYS"]


def test_image_and_dockerfile_agree_on_user_and_port():
    dockerfile = (K8S.parent.parent / "Dockerfile").read_text(encoding="utf-8")
    dep = load("deployment.yaml")
    assert f"USER {dep['spec']['template']['spec']['securityContext']['runAsUser']}" in dockerfile
    assert f"EXPOSE {container(dep)['ports'][0]['containerPort']}" in dockerfile

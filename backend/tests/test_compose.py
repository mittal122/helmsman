import pytest
from tools import compose

def test_parses_multiservice_with_dns_order_and_secret_split():
    svcs, warns = compose.parse("""
services:
  web:
    image: nginx:1.27
    ports: ["8080:80"]
    depends_on: [db]
    environment: {APP_ENV: prod, API_TOKEN: t0k}
  db:
    image: postgres:16
    environment: {POSTGRES_PASSWORD: pw}
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck: {test: ["CMD", "pg_isready"]}
""")
    assert [s["name"] for s in svcs] == ["db", "web"]        # depends_on order
    web = next(s for s in svcs if s["name"] == "web")
    db = next(s for s in svcs if s["name"] == "db")
    assert web["port"] == 80 and web["published"] is True
    assert web["env"] == {"APP_ENV": "prod"} and web["secrets"] == {"API_TOKEN": "t0k"}
    assert db["published"] is False                          # no ports:
    assert db["probe"] == {"type": "exec", "command": ["pg_isready"]}
    assert db["volumes"] == [{"name": "pgdata", "mountPath": "/var/lib/postgresql/data", "size": "1Gi"}]

def test_no_healthcheck_with_port_gets_tcp_probe():
    svcs, _ = compose.parse("services:\n  cache:\n    image: redis:7\n    ports: ['6379']\n")
    assert svcs[0]["probe"] == {"type": "tcp"}

def test_build_only_service_is_rejected():
    with pytest.raises(ValueError):
        compose.parse("services:\n  api:\n    build: ./api\n")

def test_depends_on_cycle_rejected():
    with pytest.raises(ValueError):
        compose.parse("services:\n  a:\n    image: i\n    depends_on: [b]\n"
                      "  b:\n    image: i\n    depends_on: [a]\n")

def test_invalid_service_name_rejected():
    with pytest.raises(ValueError):
        compose.parse("services:\n  Bad_Name:\n    image: i\n")

def test_networks_and_restart_warn_and_skip():
    svcs, warns = compose.parse("""
services:
  a:
    image: i
    restart: always
    networks: [back]
networks:
  back: {}
""")
    assert any("networks" in w for w in warns)
    assert any("restart" in w for w in warns)

def test_resources_and_user_and_command_mapped():
    svcs, _ = compose.parse("""
services:
  w:
    image: i
    user: "1001:1001"
    command: ["python", "app.py"]
    entrypoint: /entry.sh
    deploy:
      replicas: 3
      resources:
        limits: {cpus: "0.5", memory: 512M}
""")
    s = svcs[0]
    assert s["run_as_user"] == 1001 and s["replicas"] == 3
    assert s["args"] == ["python", "app.py"]                 # compose command -> args
    assert s["command"] == ["/entry.sh"]                     # compose entrypoint -> command
    assert s["resources"]["limits"] == {"cpu": "500m", "memory": "512Mi"}

def test_malformed_yaml_rejected():
    with pytest.raises(ValueError):
        compose.parse("services: [this is: not valid")

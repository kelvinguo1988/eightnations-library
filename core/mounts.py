"""数据目录挂载来源检测：识别 /data 是宿主机绑定路径还是 Docker 卷。

背景（2026-09-19 真实案例）：容器创建时若未显式挂载宿主机目录，因镜像
`VOLUME /data` 声明，Docker 会自动分配匿名卷（QNAP 上位于
/Container/container-station-data/lib/docker/volumes/<hash>/_data），
导致数据"存在但不在文档路径"，且删容器/清理卷时有丢失风险。
"""

import os
from typing import Optional


def data_mount() -> dict:
    """解析 /proc/self/mountinfo，返回 /data 的挂载分类。

    返回 {"kind": "bind"|"volume"|"unknown", "source": str|None}
      bind   —— 绑定了宿主机固定路径（source 即宿主机路径）：安全 ✅
      volume —— Docker（匿名）卷（source 为 docker/volumes 下哈希路径）：
                数据暂存于卷内，删容器/清理卷有丢失风险 ⚠️
      unknown —— 非 Linux（本机开发）或解析失败
    """
    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                parts = line.split(" ")
                if len(parts) > 4 and parts[4] == "/data":
                    root = parts[3]          # 宿主机上的来源路径
                    if "docker/volumes/" in root:
                        return {"kind": "volume", "source": root}
                    if root.startswith("/"):
                        return {"kind": "bind", "source": root}
                    return {"kind": "unknown", "source": root or None}
    except OSError:
        pass                                  # 非 Linux（本机开发环境）
    return {"kind": "unknown", "source": None}


def migration_commands(source: Optional[str]) -> str:
    """生成迁移指引（在 NAS SSH 中执行；首选一键脚本，诊断步骤备用）。"""
    dst = "/share/Container/eightnations/data"
    return "\n".join([
        "# 推荐：一键迁移脚本（自动发现旧卷→拷贝→重建→自检，幂等可重跑）",
        "curl -O https://raw.githubusercontent.com/kelvinguo1988/eightnations-library/main/migrate-nas.sh",
        "sh migrate-nas.sh",
        "",
        "# ── 备用：手动分步 ──",
        "# ① 诊断当前容器挂载（Type=volume 说明未挂固定路径）",
        "docker inspect eightnations --format "
        "'{{range .Mounts}}{{.Type}} {{.Name}} {{.Source}} -> {{.Destination}}{{println}}{{end}}'",
        "# ② 确认固定路径现状",
        f"ls -la {dst}",
        "# ③ 固定路径已有数据：删未挂载容器，用 compose 重建",
        "cd /share/Container/eightnations && docker compose down && docker compose up -d",
        "# ④ 固定路径为空：从旧卷拷贝（<旧卷ID>=doctor 报错里的哈希）",
        "#    docker run --rm -v <旧卷ID>:/from -v " + dst + ":/to "
        "ghcr.io/kelvinguo1988/eightnations-library:latest sh -c 'cp -a /from/. /to/'",
        "# ⑤ 校验",
        "docker exec eightnations python3 manage.py doctor",
    ])

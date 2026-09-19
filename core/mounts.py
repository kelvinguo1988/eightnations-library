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
    """生成迁移指引命令（在 NAS SSH 中执行）。

    注意场景：匿名卷警告可能在"重建后仍未挂载"时再次出现（新哈希），
    此时绝不能盲目 docker cp 当前容器（可能用空卷覆盖已迁移数据）——
    先诊断容器挂载与固定路径现状，再决定拷贝方向。
    """
    dst = "/share/Container/eightnations/data"
    return "\n".join([
        "# ① 诊断：当前容器的挂载方式（若 Type=volume 说明又没挂上固定路径）",
        "docker inspect eightnations --format "
        "'{{range .Mounts}}{{.Type}} {{.Name}} {{.Source}} -> {{.Destination}}{{println}}{{end}}'",
        "#    同时列出所有容器（注意是否有同名/旧容器并存）",
        "docker ps -a --format '{{.Names}}  {{.Status}}'",
        "",
        "# ② 诊断：固定路径里是否已有迁移好的数据",
        f"ls -la {dst}",
        "",
        "# ③ 若固定路径已有数据 → 问题只是容器未挂载：",
        "#    删除当前未挂载的容器，用仓库 compose 重建（compose 内含 volumes 段）",
        "cd /share/Container/eightnations && docker compose down && docker compose up -d",
        "",
        "# ④ 若固定路径为空 → 从旧匿名卷拷贝（<旧卷ID> 用 docker volume ls 里的真实值，",
        "#    即 doctor 报错里显示的那个哈希）：",
        "#    docker run --rm -v <旧卷ID>:/from -v " + dst + ":/to ghcr.io/kelvinguo1988/eightnations-library:latest sh -c 'cp -a /from/. /to/'",
        "",
        "# ⑤ 校验：应全绿，且本面板显示 ✓ 已绑定固定路径",
        "docker exec eightnations python3 manage.py doctor",
    ])

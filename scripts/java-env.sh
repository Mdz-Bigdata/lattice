#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# Source this Bash 3.2-compatible file, then call setup_java RUNTIME_DIR.
# This helper changes only the calling shell's JAVA_HOME and PATH.
_lattice_supported_jdk() {
    local candidate="$1" java_version javac_version major
    [ -x "$candidate/bin/java" ] && [ -x "$candidate/bin/javac" ] || return 1
    java_version=$("$candidate/bin/java" -version 2>&1) || return 1
    java_version=$(printf '%s\n' "$java_version" | sed -n 's/.*version "\([0-9][0-9]*\).*/\1/p')
    javac_version=$("$candidate/bin/javac" -version 2>&1) || return 1
    javac_version=$(printf '%s\n' "$javac_version" | sed -n 's/^javac \([0-9][0-9]*\).*/\1/p')
    for major in "$java_version" "$javac_version"; do
        case "$major" in
            ''|*[!0-9]*) return 1 ;;
        esac
        [ "$major" -ge 21 ] || return 1
    done
}

setup_java() {
    local runtime_dir="${1:-}" candidate selected_java="" detected_home=""
    local maven_version="" mac_java_home=""
    if [ -z "$runtime_dir" ]; then
        printf '%s\n' '错误：setup_java 需要项目运行目录参数。' >&2
        return 1
    fi

    # Respect a compatible explicit JAVA_HOME; prefer the installed JDK 21
    # over macOS's /usr/bin/java launcher, which may select an older JDK.
    for candidate in "${JAVA_HOME:-}" \
        /opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
        /usr/local/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
        "$runtime_dir/jdk" "$runtime_dir/java" \
        "$runtime_dir"/jdk*/Contents/Home "$runtime_dir"/jdk*; do
        if [ -n "$candidate" ] && _lattice_supported_jdk "$candidate"; then
            selected_java="$candidate"
            break
        fi
    done

    if [ -z "$selected_java" ] && [ -x /usr/libexec/java_home ]; then
        mac_java_home=$(/usr/libexec/java_home -v '21+' 2>/dev/null) || mac_java_home=""
        if [ -n "$mac_java_home" ] && _lattice_supported_jdk "$mac_java_home"; then
            selected_java="$mac_java_home"
        fi
    fi

    if [ -z "$selected_java" ] && command -v java >/dev/null 2>&1; then
        detected_home=$(java -XshowSettings:properties -version 2>&1 | \
            sed -n 's/^[[:space:]]*java.home = //p') || detected_home=""
        if [ -n "$detected_home" ] && _lattice_supported_jdk "$detected_home"; then
            selected_java="$detected_home"
        fi
    fi

    if [ -z "$selected_java" ]; then
        for candidate in /usr/lib/jvm/* /Library/Java/JavaVirtualMachines/*/Contents/Home \
            /opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home \
            /usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home; do
            if _lattice_supported_jdk "$candidate"; then
                selected_java="$candidate"
                break
            fi
        done
    fi

    if [ -z "$selected_java" ]; then
        printf '%s\n' '错误：未找到可运行的 JDK 21 或更高版本（必须包含 java 和 javac）。' >&2
        printf '请安装 JDK 21 并设置 JAVA_HOME，或将其解压至 %s/jdk 后重新启动。\n' "$runtime_dir" >&2
        return 1
    fi

    # Convert a relative runtime path into a stable absolute JAVA_HOME.
    JAVA_HOME=$(cd "$selected_java" && pwd -P) || return 1
    export JAVA_HOME
    case "${PATH:-}" in
        "$JAVA_HOME/bin"|"$JAVA_HOME/bin":*) ;;
        *) PATH="$JAVA_HOME/bin:${PATH:-}" ;;
    esac
    export PATH

    if ! command -v mvn >/dev/null 2>&1; then
        for candidate in "$runtime_dir/maven" "$runtime_dir"/apache-maven-* \
            /opt/homebrew/opt/maven/libexec /usr/local/opt/maven/libexec; do
            if [ -x "$candidate/bin/mvn" ]; then
                candidate=$(cd "$candidate" && pwd -P) || return 1
                PATH="$candidate/bin:$PATH"
                export PATH
                break
            fi
        done
    fi
    if ! command -v mvn >/dev/null 2>&1; then
        printf '%s\n' '错误：未找到 Maven（mvn）。' >&2
        printf '请安装 Apache Maven 并将其 bin 加入 PATH，或解压至 %s/maven 后重新启动。\n' "$runtime_dir" >&2
        return 1
    fi
    if ! maven_version=$(mvn -version 2>&1); then
        printf '%s\n' '错误：Maven 无法在选定的 JDK 上运行，请检查 Maven 安装和环境设置。' >&2
        printf '%s\n' "$maven_version" >&2
        return 1
    fi
    printf 'Java 环境已就绪：%s\n' "$JAVA_HOME"
    printf 'Maven 已就绪：%s\n' "$(command -v mvn)"
}

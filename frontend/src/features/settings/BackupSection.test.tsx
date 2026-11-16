import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BackupSection } from "./BackupSection";

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeStatus(overrides: Record<string, unknown> = {}) {
  return {
    policy: {
      enabled: true,
      interval_hours: 24,
      retention_count: 7,
      include_files: true,
      last_auto_backup_at: 1_700_000_000,
      last_status: "succeeded",
      last_error: null,
      last_backup_id: "20261115T030000Z-1111aaaa",
    },
    next_run_at: 1_700_086_400,
    running: false,
    running_op: null,
    backups: [
      {
        backup_id: "20261115T030000Z-1111aaaa",
        kind: "auto",
        created_at: "2026-11-15T03:00:00+00:00",
        created_ts: 1_700_000_000,
        data_epoch: 1,
        includes_files: true,
        includes_env: false,
        file_count: 10,
        payload_bytes: 20480,
        archive_bytes: 8192,
      },
    ],
    total_bytes: 8192,
    data_epoch: 1,
    required_env_vars: ["SYNTHETIC_KEY"],
    missing_env_vars: [],
    last_restore: null,
    env_export_allowed: true,
    restart_available: true,
    instance_id: "inst-1",
    restarting: false,
    ...overrides,
  };
}

describe("BackupSection", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("saves policy, supports manual workspace selection backup, resets .env checkbox on export open, and handles encrypted import inspect/apply", async () => {
    let savedPolicy: Record<string, unknown> | null = null;
    let manualNowBody: Record<string, unknown> | null = null;
    let exportBody: Record<string, unknown> | null = null;
    let appliedBody: Record<string, unknown> | null = null;

    if (!URL.createObjectURL) {
      Object.defineProperty(URL, "createObjectURL", {
        value: vi.fn(() => "blob:synthetic"),
        configurable: true,
      });
    }
    if (!URL.revokeObjectURL) {
      Object.defineProperty(URL, "revokeObjectURL", {
        value: vi.fn(),
        configurable: true,
      });
    }
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method || "GET";
      if (url === "/api/backups" && method === "GET") {
        return jsonResponse(makeStatus());
      }
      if (url.startsWith("/api/backups/workspace") && method === "GET") {
        if (url.includes("path=reports")) {
          return jsonResponse({
            exists: true,
            current_path: "reports",
            parent_path: "",
            entries: [
              { path: "reports/deliverable.md", name: "deliverable.md", is_dir: false, size: 1200, mtime: 1_700_000_000 },
            ],
            truncated: false,
          });
        }
        return jsonResponse({
          exists: true,
          current_path: "",
          parent_path: null,
          entries: [
            { path: "reports", name: "reports", is_dir: true, size: 0, mtime: 1_700_000_000 },
          ],
          truncated: false,
        });
      }
      if (url === "/api/backups/policy" && method === "PUT") {
        savedPolicy = JSON.parse(String(init?.body || "{}"));
        return jsonResponse({ ok: true, policy: { ...makeStatus().policy, ...savedPolicy } });
      }
      if (url === "/api/backups/now" && method === "POST") {
        manualNowBody = JSON.parse(String(init?.body || "{}"));
        return jsonResponse({
          ok: true,
          backup: {
            backup_id: "20261115T031000Z-aaaa1111",
            kind: "manual",
            created_at: "2026-11-15T03:10:00+00:00",
            created_ts: 1_700_000_600,
            data_epoch: 1,
            included_scopes: manualNowBody?.scopes || ["prompt"],
            includes_files: Boolean(manualNowBody?.include_files),
            includes_env: false,
            includes_workspace: Boolean((manualNowBody?.workspace_paths as string[] | undefined)?.length),
            workspace_file_count: (manualNowBody?.workspace_paths as string[] | undefined)?.length || 0,
            file_count: 5,
            payload_bytes: 4096,
            archive_bytes: 2048,
          },
        });
      }
      if (url === "/api/backups/export" && method === "POST") {
        exportBody = JSON.parse(String(init?.body || "{}"));
        return new Response(new Uint8Array([1, 2, 3]), {
          status: 200,
          headers: {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": "attachment; filename*=UTF-8''migration.tbackup",
          },
        });
      }
      if (url === "/api/backups/import/inspect" && method === "POST") {
        const form = init?.body as FormData;
        if (!form.get("passphrase")) {
          return jsonResponse({ detail: "请输入口令", code: "passphrase_required" }, 422);
        }
        return jsonResponse({
          import_token: "a".repeat(32),
          source_type: "upload",
          backup_id: "20261115T040000Z-2222bbbb",
          backup_created_at: "2026-11-15T04:00:00+00:00",
          data_epoch: 1,
          encrypted: true,
          included_scopes: ["config", "data", "prompt", "skills", "plugins"],
          includes_files: true,
          includes_env: true,
          env_key_count: 2,
          has_files: true,
          files_count: 3,
          files_bytes: 4096,
          has_workspace: true,
          workspace_files_count: 1,
          workspace_bytes: 1200,
          workspace_sample_paths: ["reports/deliverable.md"],
          file_count: 15,
          payload_bytes: 40960,
          modules: ["config", "data", "prompt", "workspace"],
          required_env_vars: ["SYNTHETIC_KEY", "EXTRA_KEY"],
          missing_if_keep_env: ["EXTRA_KEY"],
          missing_if_apply_env: [],
        });
      }
      if (url === "/api/backups/import/apply" && method === "POST") {
        appliedBody = JSON.parse(String(init?.body || "{}"));
        return jsonResponse(
          {
            ok: true,
            state: "waiting",
            instance_id: "inst-1",
            pre_backup_id: "20261115T040100Z-3333cccc",
            apply_env: true,
            apply_files: true,
            apply_workspace: true,
          },
          202,
        );
      }
      return jsonResponse({});
    });

    render(<BackupSection />);
    await waitFor(() => {
      expect(screen.getByText("20261115T030000Z-1111aaaa")).toBeInTheDocument();
    });

    // Save interval + retention
    fireEvent.change(screen.getByLabelText("自定义备份间隔小时数"), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText("自动备份保留份数"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "保存策略" }));
    await waitFor(() => {
      expect(savedPolicy).toEqual({
        enabled: true,
        interval_hours: 12,
        retention_count: 5,
        include_files: true,
      });
    });

    // Open manual backup dialog -> uncheck "插件" -> browse workspace -> drill into reports -> select deliverable.md -> create
    fireEvent.click(screen.getByRole("button", { name: "立即备份" }));
    const pluginsCheckbox = await screen.findByLabelText(/插件/);
    fireEvent.click(pluginsCheckbox);
    fireEvent.click(screen.getByRole("button", { name: /勾选 workspace\/ 产出文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "打开" }));
    const deliverableCheckbox = await screen.findByLabelText("选择 workspace/reports/deliverable.md");
    fireEvent.click(deliverableCheckbox);
    fireEvent.click(screen.getByRole("button", { name: "创建本机快照" }));
    await waitFor(() => {
      expect(manualNowBody).toEqual({
        include_files: true,
        scopes: ["config", "data", "prompt", "skills"],
        workspace_paths: ["reports/deliverable.md"],
      });
    });

    // Open export dialog -> verify .env is unchecked by default -> check .env -> enter passphrase -> export
    fireEvent.click(screen.getByRole("button", { name: /导出迁移包/ }));
    const envCheckbox = await screen.findByLabelText(/包含凭据文件/);
    expect(envCheckbox).not.toBeChecked();
    fireEvent.click(envCheckbox);
    fireEvent.change(screen.getByPlaceholderText("输入口令…"), { target: { value: "passphrase-1234" } });
    fireEvent.change(screen.getByPlaceholderText("再次输入口令…"), { target: { value: "passphrase-1234" } });
    fireEvent.click(screen.getByRole("button", { name: "生成并下载迁移包" }));
    await waitFor(() => {
      expect(exportBody).toEqual({
        include_files: true,
        include_env: true,
        scopes: ["config", "data", "prompt", "skills", "plugins"],
        passphrase: "passphrase-1234",
      });
    });

    // Reopen export dialog -> verify .env is STILL unchecked automatically on every manual open
    fireEvent.click(screen.getByRole("button", { name: /导出迁移包/ }));
    expect(await screen.findByLabelText(/包含凭据文件/)).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    // Upload encrypted .tbackup -> handles passphrase_required -> preview -> apply
    const fileInput = screen.getByLabelText("选择迁移包文件");
    const fakeArchive = new File([new Uint8Array([1, 2, 3])], "migration.tbackup", {
      type: "application/octet-stream",
    });
    fireEvent.change(fileInput, { target: { files: [fakeArchive] } });
    fireEvent.click(screen.getByRole("button", { name: "校验并预览导入" }));

    const decryptInput = await screen.findByPlaceholderText("输入解密口令…");
    fireEvent.change(decryptInput, { target: { value: "passphrase-1234" } });
    fireEvent.click(screen.getByRole("button", { name: "解密并预览" }));

    expect(await screen.findByText("确认导入恢复")).toBeInTheDocument();
    expect(screen.getByText(/含加密 \.env（2 项）/)).toBeInTheDocument();
    expect(screen.getByText("workspace/reports/deliverable.md")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认导入并重启恢复" }));

    await waitFor(() => {
      expect(appliedBody).toEqual({
        import_token: "a".repeat(32),
        apply_files: true,
        apply_env: true,
        apply_workspace: true,
        create_pre_backup: true,
      });
    });
  });

  it("disables .env export checkbox when web auth is not enabled", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(makeStatus({ env_export_allowed: false })),
    );

    render(<BackupSection />);
    await waitFor(() => {
      expect(screen.getByText("20261115T030000Z-1111aaaa")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /导出迁移包/ }));
    const envCheckbox = await screen.findByLabelText(/包含凭据文件/);
    expect(envCheckbox).toBeDisabled();
  });

  it("wraps form switches in a relative container so hidden bubble inputs never escape to document root", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(makeStatus()));
    const { container } = render(<BackupSection />);
    await waitFor(() => {
      expect(screen.getByLabelText("开启定期自动备份")).toBeInTheDocument();
    });

    const hiddenInputs = Array.from(
      container.querySelectorAll('form input[type="checkbox"][aria-hidden="true"]'),
    );
    expect(hiddenInputs.length).toBeGreaterThanOrEqual(2);
    for (const input of hiddenInputs) {
      expect(input.parentElement?.className).toContain("relative");
    }
  });
});

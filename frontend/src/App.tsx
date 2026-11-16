import { useEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "./styles/index.css";
import { ToastViewport } from "./components/ui/Toast";
import { TooltipProvider } from "./components/ui/Tooltip";
import { AuthGate } from "./features/auth/AuthGate";
import { useAuthStore } from "./features/auth/authStore";
import { useChatStore } from "./features/chat/chatStore";
import { CommandPalette } from "./features/workbench/CommandPalette";
import { refreshSubagentStatus } from "./features/workbench/layoutStore";
import { WorkbenchLayout } from "./features/workbench/WorkbenchLayout";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

export function App() {
  const boot = useAuthStore((s) => s.boot);
  const phase = useAuthStore((s) => s.phase);
  const applySettings = useChatStore((s) => s.applySettings);
  const resetSessionState = useChatStore((s) => s.resetSessionState);

  useEffect(() => {
    void boot().then((settings) => {
      if (settings) {
        applySettings(settings);
      }
    });
  }, [boot, applySettings]);

  useEffect(() => {
    if (phase === "auth_required") {
      resetSessionState();
    } else if (phase === "authenticated") {
      void refreshSubagentStatus();
    }
  }, [phase, resetSessionState]);

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <AuthGate />
        <WorkbenchLayout />
        <CommandPalette />
        <ToastViewport />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

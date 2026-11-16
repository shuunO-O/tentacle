import * as RadixDialog from "@radix-ui/react-dialog";
import { ExternalLink, X } from "lucide-react";
import { mediaContentUrl } from "../../api/client";

export interface LightboxImage {
  localId: string;
  filename?: string;
}

export function MediaLightbox({
  image,
  onClose,
}: {
  image: LightboxImage | null;
  onClose: () => void;
}) {
  const src = image ? mediaContentUrl(image.localId) : "";

  return (
    <RadixDialog.Root open={Boolean(image)} onOpenChange={(open) => !open && onClose()}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/82 backdrop-blur-xs" />
        <RadixDialog.Content className="fixed inset-0 z-50 flex flex-col items-center justify-center p-4 focus:outline-none">
          <div className="mb-3 flex w-full max-w-5xl items-center justify-between text-xs text-white/80">
            <RadixDialog.Title className="truncate font-medium">
              {image?.filename || "图片预览"}
            </RadixDialog.Title>
            <div className="flex items-center gap-2">
              {src && (
                <a
                  href={src}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 rounded-lg border border-white/20 bg-white/10 px-2.5 py-1 hover:bg-white/20"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  <span>新窗口打开</span>
                </a>
              )}
              <button
                type="button"
                onClick={onClose}
                aria-label="关闭预览"
                className="rounded-lg border border-white/20 bg-white/10 p-1.5 hover:bg-white/20"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
          {src && (
            <img
              src={src}
              alt={image?.filename || "图片"}
              className="max-h-[82vh] max-w-[92vw] rounded-xl object-contain shadow-2xl"
              onClick={onClose}
            />
          )}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

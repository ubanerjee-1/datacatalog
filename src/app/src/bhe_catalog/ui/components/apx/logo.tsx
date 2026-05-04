import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { fetchBranding } from "@/lib/api-client";

interface LogoProps {
  to?: string;
  className?: string;
  showText?: boolean;
}

export function Logo({ to = "/", className = "", showText = true }: LogoProps) {
  const { data } = useQuery({
    queryKey: ["branding"],
    queryFn: fetchBranding,
    staleTime: 60_000,
    retry: 1,
  });

  const catalogName = (data?.catalog_name || "").trim() || __APP_NAME__;
  const logoUrl = (data?.logo_url || "").trim();

  useEffect(() => {
    if (typeof document !== "undefined" && catalogName) {
      document.title = catalogName;
    }
  }, [catalogName]);

  const imgSrc = logoUrl || "/logo.svg";
  const isFallback = !logoUrl;

  const content = (
    <div className={`flex items-center gap-2 ${className}`}>
      <img
        src={imgSrc}
        alt={catalogName}
        className={
          isFallback
            ? "h-6 w-6 text-primary border border-primary rounded-sm"
            : "h-7 w-7 object-contain rounded-sm"
        }
        onError={(e) => {
          // External logo URLs (Clearbit, user-pasted) can fail to load. Fall
          // back to the bundled placeholder rather than showing a broken image.
          const img = e.currentTarget;
          if (img.src.endsWith("/logo.svg")) return;
          img.src = "/logo.svg";
          img.className = "h-6 w-6 text-primary border border-primary rounded-sm";
        }}
      />
      {showText && (
        <span className="font-semibold text-lg truncate max-w-[180px]" title={catalogName}>
          {catalogName}
        </span>
      )}
    </div>
  );

  if (to) {
    return (
      <Link to={to} className="hover:opacity-80 transition-opacity">
        {content}
      </Link>
    );
  }

  return content;
}

export default Logo;

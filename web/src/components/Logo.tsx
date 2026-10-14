import Image from "next/image";

// The MCCIA logo (public/mccia-logo.png, 268 x 72). Its letters are blue and green,
// so it needs a light background: put it on white, never on the navy sidebar itself.
export default function Logo({ width = 168, className = "" }: { width?: number; className?: string }) {
  return (
    <Image
      src="/mccia-logo.png"
      alt="MCCIA"
      width={width}
      height={Math.round((width * 72) / 268)}
      priority
      unoptimized
      className={className}
    />
  );
}

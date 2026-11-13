import Image from "next/image";

// The MCCIA logo (public/mccia-logo.png, 268 x 72). Its letters are blue and green,
// so it needs a light background: it is a transparent PNG, so it sits straight on the white page or the light grey sidebar, without a box.
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

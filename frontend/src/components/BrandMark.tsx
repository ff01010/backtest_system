interface BrandMarkProps {
  size?: number;
  className?: string;
}

/**
 * 青衡品牌标识：四根递增的行情柱，落在深蓝渐变方块里。
 * 通过 size 控制整体尺寸，内部图标按比例缩放，避免在不同入口重复硬编码。
 */
export default function BrandMark({ size = 30, className = '' }: BrandMarkProps) {
  return (
    <span
      className={`app-brand-mark${className ? ` ${className}` : ''}`}
      aria-hidden="true"
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.3) }}
    >
      <svg viewBox="0 0 24 24" width={Math.round(size * 0.56)} height={Math.round(size * 0.56)} fill="none">
        <path
          d="M4.6 18.4V13.1M9.5 18.4V9.4M14.4 18.4V11.3M19.4 18.4V5.6"
          stroke="currentColor"
          strokeWidth="2.1"
          strokeLinecap="round"
        />
      </svg>
    </span>
  );
}

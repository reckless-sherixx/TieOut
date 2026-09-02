import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const buttonVariants = cva(
  // Every button is a block with an ink border and a hard offset shadow, and
  // the press is the surface travelling into that shadow. The old
  // `translate-y-px` nudge is gone: two competing press affordances read as a
  // bug. `brut-press` owns hover, active and reduced-motion in globals.css.
  "group/button brut-press inline-flex shrink-0 items-center justify-center border-[length:var(--border-w)] border-[var(--ink)] bg-clip-padding text-sm font-semibold whitespace-nowrap outline-none select-none disabled:pointer-events-none disabled:opacity-50 disabled:shadow-none aria-invalid:border-destructive [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground",
        outline: "bg-background text-foreground",
        secondary: "bg-surface text-surface-foreground",
        // The one variant without a block: used inside dense rows where a
        // shadow per row would be noise rather than depth.
        ghost:
          "border-transparent shadow-none hover:bg-muted hover:text-foreground aria-expanded:bg-muted",
        destructive: "bg-error-surface text-error-fg",
        // Not a block, and not the amber fill: link text uses --brand-strong,
        // the 5.14:1 refit, because --brand itself is 1.99:1 as text.
        link: "border-transparent shadow-none text-[var(--brand-strong)] underline underline-offset-4",
      },
      size: {
        default:
          "h-8 gap-1.5 px-2.5 has-data-[icon=inline-end]:pr-2 has-data-[icon=inline-start]:pl-2",
        xs: "h-6 gap-1 rounded-[min(var(--radius-md),10px)] px-2 text-xs in-data-[slot=button-group]:rounded-lg has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 [&_svg:not([class*='size-'])]:size-3",
        sm: "h-7 gap-1 rounded-[min(var(--radius-md),12px)] px-2.5 text-[0.8rem] in-data-[slot=button-group]:rounded-lg has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 [&_svg:not([class*='size-'])]:size-3.5",
        lg: "h-9 gap-1.5 px-2.5 has-data-[icon=inline-end]:pr-2 has-data-[icon=inline-start]:pl-2",
        icon: "size-8",
        "icon-xs":
          "size-6 rounded-[min(var(--radius-md),10px)] in-data-[slot=button-group]:rounded-lg [&_svg:not([class*='size-'])]:size-3",
        "icon-sm":
          "size-7 rounded-[min(var(--radius-md),12px)] in-data-[slot=button-group]:rounded-lg",
        "icon-lg": "size-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return (
    <ButtonPrimitive
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }

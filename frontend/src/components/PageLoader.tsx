/**
 * The loading state, in one place. It was copy-pasted into six pages, each
 * of which also wrapped it in its own full-height dark background — which
 * is now the shell's job, so this is just the spinner.
 */
export default function PageLoader() {
  return (
    <div className="flex items-center justify-center py-32">
      <div className="w-6 h-6 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
    </div>
  )
}

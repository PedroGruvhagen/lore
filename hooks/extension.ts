// EXTENSION POINT (intentionally empty, called from nowhere).
// A later version could let an EU-hosted or local model help decide whether a candidate is
// worth saving to Lore. Nothing is implemented behind it and the mod makes no model call.
export function decideWhatToSave(_candidate: string): 'save' | 'skip' | undefined {
  return undefined
}

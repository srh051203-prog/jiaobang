export type HandoffGuardTier = 'checkpoint' | 'evaluate' | 'reevaluate';

export type HandoffGuardThreshold = {
  /** The model window the threshold was read against. */
  window: number;
  /** The token count at which auto-compaction runs (the window when it is off). */
  value: number;
  /** The compaction count when it was read, so a compaction refreshes it. */
  compactions: number;
  /** Checks answered from this reading; at REREAD_AFTER it is read again. */
  uses: number;
};

export type HandoffGuardCompaction = {
  count: number;
  trigger: 'manual' | 'auto';
  tokensBefore?: number;
};

/** The last saving point asked for a checkpoint and none has been seen written. */
export type HandoffGuardAwaiting = {
  /** Context tokens when that tier spoke. */
  tokens: number;
};

/** What a session's tiers and compactions came to, kept in `$.store` by session id. */
export type HandoffGuardSaved = Record<string, { fired: HandoffGuardTier[]; compactions: number }>;

declare module 'claude-code' {
  interface PluginState {
    'handoff-guard': {
      fired: HandoffGuardTier[];
      threshold: HandoffGuardThreshold;
      compactions: number;
      pending: HandoffGuardCompaction | null;
      /** Context tokens when the last compaction began; 0 once counted. */
      before: number;
      /** The session id the values above belong to. */
      owner: string;
      awaiting: HandoffGuardAwaiting | null;
    };
  }
}

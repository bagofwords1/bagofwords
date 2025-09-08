export type ThemeTokens = {
  palette: string[];
  background: string;
  textColor: string;
  // Card/container surfaces
  cardBackground?: string;
  cardBorder?: string;
  // Typography
  fontFamily?: string;
  headingFontFamily?: string;
  axis?: {
    xLabelColor?: string;
    xLineColor?: string;
    yLabelColor?: string;
    yLineColor?: string;
    gridLineColor?: string;
  };
  legend?: { textColor?: string };
  grid?: { top?: string; bottom?: string; left?: string; right?: string };
  tooltip?: Record<string, any>;
  animation?: { duration?: number; easing?: string };
};

export type ComponentOverrides = Record<string, Record<string, any>>;

export type ThemeDefinition = {
  tokens: ThemeTokens;
  componentOverrides?: ComponentOverrides;
};



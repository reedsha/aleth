// ui/js/dom.js — DOM element cache initialization and HTML escaping helper.
import { DOM } from "./store.js";

export function initDOMElements() {
  DOM.emptyStateContainer = document.getElementById("emptyStateContainer");
  DOM.executionStage = document.getElementById("executionStage");
  DOM.cardsGrid = document.getElementById("cardsGrid");

  // Polymorphic result view: a full-stage overlay the centre stage mounts on the
  // terminal event, chosen by the action that just ran (ui/js/result-view.js).
  DOM.resultView = document.getElementById("resultView");
  DOM.resultIcon = document.getElementById("resultIcon");
  DOM.resultKind = document.getElementById("resultKind");
  DOM.resultTitle = document.getElementById("resultTitle");
  DOM.resultMetrics = document.getElementById("resultMetrics");
  DOM.resultBody = document.getElementById("resultBody");
  DOM.btnCloseResultView = document.getElementById("btnCloseResultView");

  DOM.cardArchitect = document.getElementById("cardArchitect");
  DOM.architectLogs = document.getElementById("architectLogs");
  DOM.architectStreamContent = document.getElementById("architectStreamContent");
  DOM.architectStatusBadge = document.getElementById("architectStatusBadge");
  DOM.btnCloseArchitect = document.getElementById("btnCloseArchitect");
  DOM.architectErrorBadge = document.getElementById("architectErrorBadge");
  DOM.architectErrorMsg = document.getElementById("architectErrorMsg");
  DOM.btnRetryArchitect = document.getElementById("btnRetryArchitect");
  DOM.architectSummary = document.getElementById("architectSummary");

  DOM.cardCoder = document.getElementById("cardCoder");
  DOM.coderLogs = document.getElementById("coderLogs");
  DOM.coderStreamContent = document.getElementById("coderStreamContent");
  DOM.coderStatusBadge = document.getElementById("coderStatusBadge");
  DOM.coderCardName = document.getElementById("coderCardName");
  DOM.coderCardModel = document.getElementById("coderCardModel");
  DOM.btnCloseCoder = document.getElementById("btnCloseCoder");
  DOM.coderErrorBadge = document.getElementById("coderErrorBadge");
  DOM.coderErrorMsg = document.getElementById("coderErrorMsg");
  DOM.btnRetryCoder = document.getElementById("btnRetryCoder");
  DOM.coderSummary = document.getElementById("coderSummary");

  // Action Control Panel (Replacing Chatbox)
  DOM.actionControlPanelContainer = document.getElementById("actionControlPanelContainer");
  DOM.actionPanelLockOverlay = document.getElementById("actionPanelLockOverlay");
  DOM.actionDockCard = document.getElementById("actionDockCard");
  // Command palette: the six intents the dock strip used to hold.
  DOM.commandPaletteOverlay = document.getElementById("commandPaletteOverlay");
  DOM.commandPaletteInput = document.getElementById("commandPaletteInput");
  DOM.commandPaletteList = document.getElementById("commandPaletteList");
  DOM.btnCommandPalette = document.getElementById("btnCommandPalette");
  // Stop lives in the top bar now that the dock's read-only status row is gone; the completion
  // figure is rendered by the workbench's bento progress tile, not the top bar.
  DOM.btnStopRun = document.getElementById("btnStopRun");

  // Docked command drawer (the parameter form of the action strip above it)
  DOM.actionDrawerPanel = document.getElementById("actionDrawerPanel");
  DOM.btnCloseParamModal = document.getElementById("btnCloseParamModal");
  DOM.btnCancelParamModal = document.getElementById("btnCancelParamModal");
  DOM.btnConfirmActionParam = document.getElementById("btnConfirmActionParam");
  DOM.btnConfirmActionText = document.getElementById("btnConfirmActionText");
  DOM.paramModalIcon = document.getElementById("paramModalIcon");
  DOM.paramModalTitle = document.getElementById("paramModalTitle");
  DOM.paramModalSubtitle = document.getElementById("paramModalSubtitle");
  DOM.paramActionTypeBadge = document.getElementById("paramActionTypeBadge");
  DOM.paramPresetDescription = document.getElementById("paramPresetDescription");
  DOM.paramTargetTaskCard = document.getElementById("paramTargetTaskCard");
  DOM.paramTargetBadge = document.getElementById("paramTargetBadge");
  DOM.paramTargetTitle = document.getElementById("paramTargetTitle");
  DOM.paramBugSection = document.getElementById("paramBugSection");
  DOM.inputBugDescription = document.getElementById("inputBugDescription");
  DOM.inputBugAttachment = document.getElementById("inputBugAttachment");
  DOM.lblAttachmentName = document.getElementById("lblAttachmentName");
  DOM.bugValidationMsg = document.getElementById("bugValidationMsg");
  DOM.lblActionCustomInstructions = document.getElementById("lblActionCustomInstructions");
  DOM.customValidationMsg = document.getElementById("customValidationMsg");
  DOM.inputActionCustomInstructions = document.getElementById("inputActionCustomInstructions");

  // Workspace & Left Sidebar
  DOM.txtWorkspacePath = document.getElementById("txtWorkspacePath");
  DOM.btnSelectWorkspace = document.getElementById("btnSelectWorkspace");
  DOM.sidebarMainAgentsList = document.getElementById("sidebarMainAgentsList");
  DOM.sidebarCoderAgentsList = document.getElementById("sidebarCoderAgentsList");
  DOM.countMainAgents = document.getElementById("countMainAgents");
  DOM.countCoderAgents = document.getElementById("countCoderAgents");

  // Collapsible side panes and the panels that live in the left one
  DOM.leftSidebar = document.getElementById("leftSidebar");
  DOM.rightPlanSidebar = document.getElementById("rightPlanSidebar");
  DOM.btnToggleLeftSidebar = document.getElementById("btnToggleLeftSidebar");
  DOM.btnToggleRightSidebar = document.getElementById("btnToggleRightSidebar");
  DOM.sidebarTree = document.getElementById("sidebarTree");
  DOM.countTreeFiles = document.getElementById("countTreeFiles");
  DOM.btnExpandFilesModal = document.getElementById("btnExpandFilesModal");
  DOM.sidebarEnvList = document.getElementById("sidebarEnvList");
  DOM.countEnvVars = document.getElementById("countEnvVars");
  DOM.tabSidebarAgents = document.getElementById("tabSidebarAgents");
  DOM.tabSidebarPlans = document.getElementById("tabSidebarPlans");
  DOM.tabSidebarFiles = document.getElementById("tabSidebarFiles");
  DOM.tabSidebarEnv = document.getElementById("tabSidebarEnv");
  DOM.sidebarPlansList = document.getElementById("sidebarPlansList");
  DOM.countSidebarPlans = document.getElementById("countSidebarPlans");
  DOM.btnExpandSwitchPlan = document.getElementById("btnExpandSwitchPlan");

  // Top Status Bar & Right Plan Sidebar
  DOM.txtTopActivePlanName = document.getElementById("txtTopActivePlanName");
  DOM.topActivePlanBadge = document.getElementById("topActivePlanBadge");
  DOM.txtSidebarPlanName = document.getElementById("txtSidebarPlanName");

  DOM.btnCreatePlanModal = document.getElementById("btnCreatePlanModal");
  DOM.txtPlanProgressRatio = document.getElementById("txtPlanProgressRatio");
  DOM.planProgressBarFill = document.getElementById("planProgressBarFill");
  DOM.planProgressBarActive = document.getElementById("planProgressBarActive");
  DOM.planProgressBarFailed = document.getElementById("planProgressBarFailed");
  DOM.planTreeContainer = document.getElementById("planTreeContainer");
  // Bento header above the roadmap: the plan's standing context plus its live state.
  DOM.bentoSummaryTile = document.getElementById("bentoSummaryTile");
  DOM.bentoSummaryPreview = document.getElementById("bentoSummaryPreview");
  DOM.btnBentoExpandSummary = document.getElementById("btnBentoExpandSummary");
  DOM.bentoProgressTile = document.getElementById("bentoProgressTile");
  DOM.bentoProgressChip = document.getElementById("bentoProgressChip");
  DOM.bentoStatsTile = document.getElementById("bentoStatsTile");
  DOM.bentoStatsChips = document.getElementById("bentoStatsChips");
  DOM.btnRetagUi = document.getElementById("btnRetagUi");
  DOM.taggingOverlay = document.getElementById("taggingOverlay");
  DOM.taggingTitle = document.getElementById("taggingTitle");
  DOM.taggingCount = document.getElementById("taggingCount");
  DOM.taggingCurrent = document.getElementById("taggingCurrent");
  DOM.taggingFill = document.getElementById("taggingFill");
  DOM.btnCloseTagging = document.getElementById("btnCloseTagging");

  // Plan Management Modals (Split: Switch vs Create)
  DOM.switchPlanModalOverlay = document.getElementById("switchPlanModalOverlay");
  DOM.btnCloseSwitchPlanModal = document.getElementById("btnCloseSwitchPlanModal");
  DOM.createPlanModalOverlay = document.getElementById("createPlanModalOverlay");
  DOM.btnCloseCreatePlanModal = document.getElementById("btnCloseCreatePlanModal");
  // Normalization Gate (an imported .md the plan parser cannot read)
  DOM.normalizeGateModalOverlay = document.getElementById("normalizeGateModalOverlay");
  DOM.btnCloseNormalizeGate = document.getElementById("btnCloseNormalizeGate");
  DOM.normalizeGateIssues = document.getElementById("normalizeGateIssues");
  DOM.normalizeGateCounts = document.getElementById("normalizeGateCounts");
  DOM.btnSkipNormalize = document.getElementById("btnSkipNormalize");
  DOM.btnConfirmNormalize = document.getElementById("btnConfirmNormalize");
  DOM.existingPlansList = document.getElementById("existingPlansList");
  DOM.inputNewPlanName = document.getElementById("inputNewPlanName");
  DOM.inputProjectIdea = document.getElementById("inputProjectIdea");
  DOM.btnSubmitCreatePlan = document.getElementById("btnSubmitCreatePlan");

  // Prompt Editor
  DOM.chatWorkspaceView = document.getElementById("chatWorkspaceView");
  DOM.promptEditorView = document.getElementById("promptEditorView");
  DOM.btnBackToChat = document.getElementById("btnBackToChat");
  DOM.editorAgentDisplayName = document.getElementById("editorAgentDisplayName");
  DOM.editorRolePill = document.getElementById("editorRolePill");
  DOM.editorModelBadge = document.getElementById("editorModelBadge");
  DOM.editorFilePathBadge = document.getElementById("editorFilePathBadge");
  DOM.editorToolsList = document.getElementById("editorToolsList");
  DOM.txtSystemPrompt = document.getElementById("txtSystemPrompt");
  DOM.editorLineNumbers = document.getElementById("editorLineNumbers");
  DOM.promptCharCount = document.getElementById("promptCharCount");
  DOM.btnSavePrompt = document.getElementById("btnSavePrompt");
  DOM.btnResetPrompt = document.getElementById("btnResetPrompt");

  // Files Modal & Status Bar
  DOM.filesModalOverlay = document.getElementById("filesModalOverlay");
  DOM.btnCloseFilesModal = document.getElementById("btnCloseFilesModal");
  DOM.btnCloseFilesModalFooter = document.getElementById("btnCloseFilesModalFooter");
  DOM.inputSearchWorkspaceFiles = document.getElementById("inputSearchWorkspaceFiles");
  DOM.txtFilesCountBadge = document.getElementById("txtFilesCountBadge");
  DOM.modalFilesList = document.getElementById("modalFilesList");

  // Multimodal UI Vision Section (Module 6)
  DOM.paramUiVisionSection = document.getElementById("paramUiVisionSection");
  DOM.inputUiImageAttachment = document.getElementById("inputUiImageAttachment");
  DOM.lblUiAttachmentName = document.getElementById("lblUiAttachmentName");
  DOM.uiImagePreviewCard = document.getElementById("uiImagePreviewCard");
  DOM.imgUiPreview = document.getElementById("imgUiPreview");
  DOM.btnRemoveUiPreview = document.getElementById("btnRemoveUiPreview");

  // Tiered Prompt Editor (Module 7)
  DOM.btnToggleCoreRules = document.getElementById("btnToggleCoreRules");
  DOM.coreRulesContent = document.getElementById("coreRulesContent");
  DOM.txtCoreRulesPre = document.getElementById("txtCoreRulesPre");
  DOM.lblCoreToggle = document.getElementById("lblCoreToggle");

  // Codebase & Plan Sync Audit Modal (Module 5)
  DOM.btnAuditPlanSync = document.getElementById("btnAuditPlanSync");
  DOM.auditModalOverlay = document.getElementById("auditModalOverlay");
  // Artifact DAG rail panel: the review surface for planned artifacts, its own rail panel
  // beside the audit's rather than a section inside the plan tree.
  DOM.btnDagPanel = document.getElementById("btnDagPanel");
  DOM.dagPanelOverlay = document.getElementById("dagPanel");
  DOM.btnCloseDagPanel = document.getElementById("btnCloseDagPanel");
  DOM.btnCloseAuditModal = document.getElementById("btnCloseAuditModal");
  DOM.btnCloseAuditModalFooter = document.getElementById("btnCloseAuditModalFooter");
  DOM.auditHeaderTitle = document.getElementById("auditHeaderTitle");
  DOM.auditHeaderSubtitle = document.getElementById("auditHeaderSubtitle");
  DOM.auditSummarySection = document.getElementById("auditSummarySection");
  DOM.txtAuditSummaryFull = document.getElementById("txtAuditSummaryFull");
  DOM.auditStatusPill = document.getElementById("auditStatusPill");
  DOM.auditStatusSummary = document.getElementById("auditStatusSummary");
  DOM.badgeMissingFilesCount = document.getElementById("badgeMissingFilesCount");
  DOM.badgeExistingFilesCount = document.getElementById("badgeExistingFilesCount");
  DOM.listAuditMissing = document.getElementById("listAuditMissing");
  DOM.listAuditExisting = document.getElementById("listAuditExisting");
  DOM.txtUntrackedFiles = document.getElementById("txtUntrackedFiles");
  DOM.btnSyncPlanToCode = document.getElementById("btnSyncPlanToCode");
  DOM.btnForceCodeToPlan = document.getElementById("btnForceCodeToPlan");

  // Rollback Modal (Module 8)
  DOM.rollbackModalOverlay = document.getElementById("rollbackModalOverlay");
  DOM.btnCloseRollbackModal = document.getElementById("btnCloseRollbackModal");
  DOM.btnCancelRollback = document.getElementById("btnCancelRollback");
  DOM.rollbackTargetTitle = document.getElementById("rollbackTargetTitle");
  DOM.rollbackFilesList = document.getElementById("rollbackFilesList");
  DOM.chkRollbackConfirm = document.getElementById("chkRollbackConfirm");
  DOM.btnConfirmRollbackAction = document.getElementById("btnConfirmRollbackAction");

  // Bottom Dock (resizable console housing the action toolbar)
  DOM.bottomDock = document.getElementById("bottomDock");
  DOM.dockResizeHandle = document.getElementById("dockResizeHandle");
  DOM.consoleStream = document.getElementById("consoleStream");
  DOM.btnConsoleClear = document.getElementById("btnConsoleClear");
  DOM.btnConsoleOverlay = document.getElementById("btnConsoleOverlay");
  DOM.btnConsoleDetach = document.getElementById("btnConsoleDetach");
  DOM.btnConsoleToggle = document.getElementById("btnConsoleToggle");

  // The live run (Phase 34): the interrupt control, the paused badge, the token burn readout, and
  // the steering overlay the console reveals while a run is held.
  DOM.btnInterruptRun = document.getElementById("btnInterruptRun");
  DOM.consolePausedBadge = document.getElementById("consolePausedBadge");
  DOM.tokenBurn = document.getElementById("tokenBurn");
  DOM.steerOverlay = document.getElementById("steerOverlay");
  DOM.steerInput = document.getElementById("steerInput");
  DOM.btnSteerSubmit = document.getElementById("btnSteerSubmit");
  DOM.btnSteerCancel = document.getElementById("btnSteerCancel");

  // Live preview (the workspace's generated interface, rendered in place)
  DOM.btnTogglePreview = document.getElementById("btnTogglePreview");
  DOM.previewPane = document.getElementById("previewPane");
  DOM.previewPath = document.getElementById("previewPath");
  DOM.previewFrame = document.getElementById("previewFrame");
  DOM.previewFail = document.getElementById("previewFail");
  DOM.previewFailMsg = document.getElementById("previewFailMsg");
  DOM.btnPreviewReload = document.getElementById("btnPreviewReload");
  DOM.btnPreviewRetry = document.getElementById("btnPreviewRetry");
  DOM.btnPreviewClose = document.getElementById("btnPreviewClose");

  // Plan Workbench (centre stage: the active plan as a read/write document)
  DOM.crumbWorkspace = document.getElementById("crumbWorkspace");
  DOM.crumbPlanFile = document.getElementById("crumbPlanFile");
  DOM.crumbDirty = document.getElementById("crumbDirty");
  DOM.btnWorkbenchEdit = document.getElementById("btnWorkbenchEdit");
  DOM.btnWorkbenchSave = document.getElementById("btnWorkbenchSave");
  DOM.btnWorkbenchDiscard = document.getElementById("btnWorkbenchDiscard");
  DOM.planEditorGutter = document.getElementById("planEditorGutter");
  DOM.planDoc = document.getElementById("planDoc");
  DOM.planEditorInput = document.getElementById("planEditorInput");
  DOM.txtWorkbenchMeta = document.getElementById("txtWorkbenchMeta");

  // Plan workbench views: the roadmap as a document tree, the DAG canvas, or the raw source
  DOM.btnWorkbenchTreeView = document.getElementById("btnWorkbenchTreeView");
  DOM.btnWorkbenchDagView = document.getElementById("btnWorkbenchDagView");
  DOM.btnWorkbenchRawMd = document.getElementById("btnWorkbenchRawMd");
  DOM.workbenchTreeView = document.getElementById("workbenchTreeView");
  DOM.workbenchDagView = document.getElementById("workbenchDagView");
  // Reopens the last action's result view after it has been dismissed
  DOM.btnWorkbenchResult = document.getElementById("btnWorkbenchResult");

  DOM.systemStatusDot = document.getElementById("systemStatusDot");
  DOM.systemStatusLabel = document.getElementById("systemStatusLabel");
  DOM.toastContainer = document.getElementById("toastContainer");

  // Settings modal (provider endpoint and model routes)
  DOM.btnOpenSettings = document.getElementById("btnOpenSettings");
  DOM.settingsModalOverlay = document.getElementById("settingsModalOverlay");
  DOM.btnCloseSettingsModal = document.getElementById("btnCloseSettingsModal");
  DOM.btnCancelSettings = document.getElementById("btnCancelSettings");
  DOM.btnSaveSettings = document.getElementById("btnSaveSettings");
  DOM.settingsFields = document.getElementById("settingsFields");
  DOM.settingsStatus = document.getElementById("settingsStatus");
}

export function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

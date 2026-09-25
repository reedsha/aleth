// ui/js/dom.js — DOM element cache initialization and HTML escaping helper.
function initDOMElements() {
  DOM.emptyStateContainer = document.getElementById("emptyStateContainer");
  DOM.executionStage = document.getElementById("executionStage");
  DOM.cardsGrid = document.getElementById("cardsGrid");

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
  DOM.btnActionFixBug = document.getElementById("btnActionFixBug");
  DOM.btnActionNextStep = document.getElementById("btnActionNextStep");
  DOM.btnActionUpdatePlan = document.getElementById("btnActionUpdatePlan");
  DOM.btnActionAnalyze = document.getElementById("btnActionAnalyze");
  DOM.btnActionRecommend = document.getElementById("btnActionRecommend");
  DOM.btnActionCustom = document.getElementById("btnActionCustom");
  DOM.lblNextStepTarget = document.getElementById("lblNextStepTarget");
  DOM.readonlyPlanPill = document.getElementById("readonlyPlanPill");
  DOM.txtBottomActivePlan = document.getElementById("txtBottomActivePlan");
  DOM.txtDockProgress = document.getElementById("txtDockProgress");
  DOM.btnDockStop = document.getElementById("btnDockStop");

  // Action Parameter Modal
  DOM.actionParamModalOverlay = document.getElementById("actionParamModalOverlay");
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

  // Top Status Bar & Right Plan Sidebar
  DOM.txtTopActivePlanName = document.getElementById("txtTopActivePlanName");
  DOM.topActivePlanBadge = document.getElementById("topActivePlanBadge");
  DOM.txtSidebarPlanName = document.getElementById("txtSidebarPlanName");
  DOM.btnSwitchPlan = document.getElementById("btnSwitchPlan");
  DOM.btnCreatePlanModal = document.getElementById("btnCreatePlanModal");
  DOM.txtPlanProgressRatio = document.getElementById("txtPlanProgressRatio");
  DOM.planProgressBarFill = document.getElementById("planProgressBarFill");
  DOM.planTreeContainer = document.getElementById("planTreeContainer");

  // Plan Management Modals (Split: Switch vs Create)
  DOM.switchPlanModalOverlay = document.getElementById("switchPlanModalOverlay");
  DOM.btnCloseSwitchPlanModal = document.getElementById("btnCloseSwitchPlanModal");
  DOM.createPlanModalOverlay = document.getElementById("createPlanModalOverlay");
  DOM.btnCloseCreatePlanModal = document.getElementById("btnCloseCreatePlanModal");
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
  DOM.btnNavFiles = document.getElementById("btnNavFiles");
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
  DOM.btnCloseAuditModal = document.getElementById("btnCloseAuditModal");
  DOM.btnCloseAuditModalFooter = document.getElementById("btnCloseAuditModalFooter");
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

  // Tracked-edits pane (the stage's right-hand editor split)
  DOM.diffPane = document.getElementById("diffPane");
  DOM.diffPaneTitle = document.getElementById("diffPaneTitle");
  DOM.diffPaneStat = document.getElementById("diffPaneStat");
  DOM.diffPaneBody = document.getElementById("diffPaneBody");
  DOM.btnDiffPaneClose = document.getElementById("btnDiffPaneClose");

  // Plan Workbench (centre stage: the active plan as a read/write document)
  DOM.crumbWorkspace = document.getElementById("crumbWorkspace");
  DOM.crumbPlanFile = document.getElementById("crumbPlanFile");
  DOM.crumbDirty = document.getElementById("crumbDirty");
  DOM.txtWorkbenchStat = document.getElementById("txtWorkbenchStat");
  DOM.btnWorkbenchEdit = document.getElementById("btnWorkbenchEdit");
  DOM.btnWorkbenchSave = document.getElementById("btnWorkbenchSave");
  DOM.btnWorkbenchDiscard = document.getElementById("btnWorkbenchDiscard");
  DOM.planEditorGutter = document.getElementById("planEditorGutter");
  DOM.planDoc = document.getElementById("planDoc");
  DOM.planEditorInput = document.getElementById("planEditorInput");
  DOM.txtWorkbenchPath = document.getElementById("txtWorkbenchPath");
  DOM.txtWorkbenchMeta = document.getElementById("txtWorkbenchMeta");

  DOM.systemStatusDot = document.getElementById("systemStatusDot");
  DOM.systemStatusLabel = document.getElementById("systemStatusLabel");
  DOM.toastContainer = document.getElementById("toastContainer");
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

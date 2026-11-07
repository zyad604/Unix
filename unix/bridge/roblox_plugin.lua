-- SPDX-License-Identifier: GPL-3.0-or-later
-- Copyright (C) 2026 Larpbase
-- Unix Bridge: receives models and effects from Unix (Blender) and builds them in
-- the open place. Installed by Unix into Roblox Studio's local Plugins folder.
--
-- It talks only to this computer (127.0.0.1). It builds instances from the data it receives
-- (Parts, MeshParts with their surfaces, emitters, flipbook pictures, beams, lights, and scripts whose text is stored for the game to
-- run when you press Play; rigged meshes with their bones and their animation as poses). The plugin itself never
-- executes code sent to it: the scripts it writes are fixed text in this file.

local VERSION = "15"
local PORTS = { 7791, 7792, 7793 }

local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")
local AssetService = game:GetService("AssetService")
local ChangeHistoryService = game:GetService("ChangeHistoryService")
local Workspace = game:GetService("Workspace")

local TESTING = "UnixTesting" -- plugin setting: when a play test last said it was running

if RunService:IsRunning() then
	-- Inside a play test this copy builds nothing. It only lets the editor's copy know a test is
	-- on, so Unix can say that what it sends shows up once the test is stopped.
	if RunService:IsServer() then
		task.spawn(function()
			while true do
				pcall(function()
					plugin:SetSetting(TESTING, os.time())
				end)
				task.wait(3)
			end
		end)
	end
	return
end

local function testing()
	local ok, stamp = pcall(function()
		return plugin:GetSetting(TESTING)
	end)
	return ok and type(stamp) == "number" and os.time() - stamp < 8
end

local SESSION = HttpService:GenerateGUID(false)
local status = "starting"

-- Studio reloads this file when Unix updates it, and the copy that was already running keeps
-- going. A marker (kept out of the saved place) names the newest copy; older ones stop.
local marker
do
	local ok, coreGui = pcall(function()
		return game:GetService("CoreGui")
	end)
	if ok and coreGui then
		marker = coreGui:FindFirstChild("UnixBridgeMarker")
		if not marker then
			marker = Instance.new("StringValue")
			marker.Name = "UnixBridgeMarker"
			marker.Archivable = false
			pcall(function()
				marker.Parent = coreGui
			end)
		end
		marker.Value = SESSION
	end
end

local function current()
	return marker == nil or marker.Value == SESSION
end

-- toolbar button: shows the link state, nothing else
local toolbar = plugin:CreateToolbar("Unix")
local button = toolbar:CreateButton("Unix Bridge", "Link to Unix (Blender)", "")
button.ClickableWhenViewportHidden = true
local togglePreview -- set below, once the preview exists
button.Click:Connect(function()
	if togglePreview then
		togglePreview()
	end
	print("[Unix Bridge] v" .. VERSION .. " - " .. status)
	button:SetActive(false)
end)

---------------------------------------------------------------------------------------------
-- helpers

local cachedName, cachedAt = nil, 0

local function placeName()
	if cachedName and os.clock() - cachedAt < 30 then
		return cachedName
	end
	local name = game.Name
	if (name == nil or name == "" or name == "Game") and game.PlaceId ~= 0 then
		local ok, info = pcall(function()
			return game:GetService("MarketplaceService"):GetProductInfo(game.PlaceId)
		end)
		if ok and info and info.Name then
			name = info.Name
		end
	end
	if name == nil or name == "" then
		name = "Unsaved place"
	end
	cachedName, cachedAt = name, os.clock()
	return name
end

local function request(port, path, body)
	local ok, response = pcall(function()
		return HttpService:RequestAsync({
			Url = "http://127.0.0.1:" .. tostring(port) .. path,
			Method = "POST",
			Headers = { ["Content-Type"] = "application/json", ["X-Unix-Plugin"] = VERSION },
			Body = HttpService:JSONEncode(body),
		})
	end)
	if not ok or not response or not response.Success then
		return nil
	end
	local decoded
	local parsed = pcall(function()
		decoded = HttpService:JSONDecode(response.Body)
	end)
	if not parsed then
		return nil
	end
	return decoded
end

local B64 = {}
do
	local alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
	for i = 1, #alphabet do
		B64[string.byte(alphabet, i)] = i - 1
	end
end

-- base64 text -> buffer of bytes
local function decodeBase64(text)
	local length = #text
	local padding = 0
	if string.sub(text, length, length) == "=" then
		padding = 1
		if string.sub(text, length - 1, length - 1) == "=" then
			padding = 2
		end
	end
	local size = (length / 4) * 3 - padding
	local out = buffer.create(size)
	local position = 0
	for i = 1, length, 4 do
		local a = B64[string.byte(text, i)] or 0
		local b = B64[string.byte(text, i + 1)] or 0
		local c = B64[string.byte(text, i + 2)] or 0
		local d = B64[string.byte(text, i + 3)] or 0
		local value = a * 262144 + b * 4096 + c * 64 + d
		if position < size then
			buffer.writeu8(out, position, math.floor(value / 65536) % 256)
		end
		if position + 1 < size then
			buffer.writeu8(out, position + 1, math.floor(value / 256) % 256)
		end
		if position + 2 < size then
			buffer.writeu8(out, position + 2, value % 256)
		end
		position = position + 3
	end
	return out
end

local function color3(list)
	if not list then
		return Color3.new(0.64, 0.64, 0.64)
	end
	return Color3.new(list[1], list[2], list[3])
end

local function material(name)
	local ok, value = pcall(function()
		return Enum.Material[name]
	end)
	if ok and value then
		return value
	end
	return Enum.Material.Plastic
end

local function cframe(list)
	return CFrame.new(list[1], list[2], list[3], list[4], list[5], list[6], list[7], list[8], list[9], list[10], list[11], list[12])
end

-- where a new model should stand: on the ground in front of the Studio camera
local function dropPoint()
	local camera = Workspace.CurrentCamera
	if not camera then
		return Vector3.new(0, 0, 0)
	end
	local ahead = camera.CFrame.Position + camera.CFrame.LookVector * 40
	local hit = Workspace:Raycast(ahead + Vector3.new(0, 400, 0), Vector3.new(0, -2000, 0))
	if hit then
		return hit.Position
	end
	return Vector3.new(ahead.X, 0, ahead.Z)
end

---------------------------------------------------------------------------------------------
-- builders

local models = {} -- asset name -> { model = Model, origin = Vector3 }

-- what a surface's pictures are for -> the SurfaceAppearance property that shows them
local SURFACE_MAPS = { color = "ColorMapContent", normal = "NormalMapContent", roughness = "RoughnessMapContent",
	metalness = "MetalnessMapContent", emissive = "EmissiveMaskContent" }

-- Mesh and image assets this place already uses for shapes Unix sent before, by shape key.
-- Sending something again (or the same tree in another model) reuses them: nothing is uploaded twice.
local function uploadedBefore()
	local meshes, images, films = {}, {}, {}
	for _, item in ipairs(Workspace:GetDescendants()) do
		if item:IsA("ParticleEmitter") then
			local filmKey = item:GetAttribute("UnixFilm")
			if filmKey and item.Texture ~= "" then
				films[filmKey] = item.Texture
			end
		end
		if item:IsA("MeshPart") then
			local key = item:GetAttribute("UnixMesh")
			if key and not meshes[key] then
				local id = item:GetAttribute("UnixMeshAsset")
				if id then
					meshes[key] = "rbxassetid://" .. tostring(id)
				elseif item.MeshId ~= "" then
					meshes[key] = item.MeshId
				end
			end
			local imageKey = item:GetAttribute("UnixTexture")
			if imageKey and not images[imageKey] and item.TextureID ~= "" then
				images[imageKey] = item.TextureID
			end
		end
		if item:IsA("SurfaceAppearance") then
			for role, property in pairs(SURFACE_MAPS) do
				local pictureKey = item:GetAttribute("Unix_" .. role)
				if pictureKey and not images[pictureKey] then
					pcall(function()
						local content = item[property]
						if content.SourceType == Enum.ContentSourceType.Uri and content.Uri ~= "" then
							images[pictureKey] = content.Uri
						end
					end)
				end
			end
		end
	end
	return meshes, images, films
end

local function beginAsset(job)
	local existing = nil
	for _, child in ipairs(Workspace:GetChildren()) do
		if child:IsA("Model") and child:GetAttribute("UnixAsset") == job.asset then
			existing = child
			break
		end
	end
	local keptMeshes, keptImages, keptFilms = uploadedBefore()
	local origin
	if existing then
		-- sending the same asset again replaces it where it stands
		local stored = existing:GetAttribute("UnixOrigin")
		origin = stored or existing:GetPivot().Position
		existing:Destroy()
	elseif job.place == "camera" then
		origin = dropPoint()
	else
		origin = Vector3.new(0, 0, 0)
	end
	local model = Instance.new("Model")
	model.Name = job.asset
	model:SetAttribute("UnixAsset", job.asset)
	model:SetAttribute("UnixOrigin", origin)
	model.Parent = Workspace
	models[job.asset] = { model = model, origin = origin, images = {}, shapes = {}, keptMeshes = keptMeshes,
		keptImages = keptImages, keptFilms = keptFilms, animated = false }
	return models[job.asset]
end

local function assetOf(job)
	local entry = models[job.asset]
	if job.first or not entry or not entry.model.Parent then
		entry = beginAsset(job)
	end
	return entry
end

-- keep an object's sampled motion with the part, placed where the asset stands
local function attachTrack(part, track, entry)
	if not track or not track.frames or #track.frames < 2 then
		return
	end
	local origin = entry.origin
	local frames = table.create(#track.frames)
	for index, f in ipairs(track.frames) do
		frames[index] = { f[1] + origin.X, f[2] + origin.Y, f[3] + origin.Z, f[4], f[5], f[6], f[7], f[8], f[9], f[10], f[11], f[12] }
	end
	local value = Instance.new("StringValue")
	value.Name = "UnixTrack"
	value.Value = HttpService:JSONEncode({ fps = track.fps, frames = frames })
	value.Parent = part
	entry.animated = true
end

local SHAPES = { Ball = Enum.PartType.Ball, Cylinder = Enum.PartType.Cylinder }

local function buildParts(job, entry, report)
	for _, item in ipairs(job.parts or {}) do
		local part
		if item.shape == "Wedge" then
			part = Instance.new("WedgePart")
		else
			part = Instance.new("Part")
			if SHAPES[item.shape] then
				part.Shape = SHAPES[item.shape]
			end
		end
		part.Name = item.name
		part.Anchored = true
		part.Size = Vector3.new(item.size[1], item.size[2], item.size[3])
		part.CFrame = cframe(item.cframe) + entry.origin
		part.Color = color3(item.color)
		part.Material = material(item.material)
		part.Transparency = item.transparency or 0
		part.TopSurface = Enum.SurfaceType.Smooth
		part.BottomSurface = Enum.SurfaceType.Smooth
		attachTrack(part, item.track, entry)
		part.Parent = entry.model
		report.parts = report.parts + 1
	end
end

---------------------------------------------------------------------------------------------
-- rigs: a mesh that bones move arrives with its skin and hangs on one skeleton per armature

-- bones and weights for an EditableMesh: a vertex follows up to four bones, the weights are bytes
local function skinMesh(mesh, vertexIds, skin)
	local ids = table.create(#skin.names)
	for index, name in ipairs(skin.names) do
		local bone = { Name = name, CFrame = cframe(skin.bind[index]), Virtual = false }
		local above = skin.parents[index]
		if above > 0 then
			bone.ParentId = ids[above]
		end
		ids[index] = mesh:AddBone(bone)
	end
	local bones, shares = decodeBase64(skin.vb), decodeBase64(skin.vw)
	local wide = skin.wide == true
	for i = 0, #vertexIds - 1 do
		local held, weights = {}, {}
		for k = i * 4, i * 4 + 3 do
			local share = buffer.readu8(shares, k)
			if share > 0 then
				table.insert(held, ids[(wide and buffer.readu16(bones, k * 2) or buffer.readu8(bones, k)) + 1])
				table.insert(weights, share / 255)
			end
		end
		if #held > 0 then
			mesh:SetVertexBones(vertexIds[i + 1], held)
			mesh:SetVertexBoneWeights(vertexIds[i + 1], weights)
		end
	end
end

-- Roblox makes no Bone instances for a mesh built here, and a mesh finds its bones by name in the
-- part it is welded to. So one invisible part per armature holds the bones, in the armature's
-- hierarchy, and every piece of every mesh of that rig is welded to it: one body, one skeleton.
local function rigFor(entry, name)
	entry.rigParts = entry.rigParts or {}
	local holder = entry.rigParts[name]
	if holder and holder.Parent then
		return holder
	end
	local data = entry.rigs and entry.rigs[name]
	if not data then
		return nil
	end
	holder = Instance.new("Part")
	holder.Name = name
	holder.Anchored = true
	holder.CanCollide = false
	holder.CanQuery = false
	holder.CanTouch = false
	holder.Transparency = 1
	holder.Size = Vector3.new(0.2, 0.2, 0.2)
	holder.CFrame = cframe(data.cframe) + entry.origin
	holder:SetAttribute("UnixRig", name)
	local made, names = table.create(#data.bones), table.create(#data.bones)
	for index, item in ipairs(data.bones) do -- parents come before their children
		local bone = Instance.new("Bone")
		bone.Name = item.name
		local world = cframe(item.cframe) + entry.origin
		local above = made[item.parent]
		if above then
			bone.CFrame = above.world:ToObjectSpace(world)
			bone.Parent = above.bone
		else
			bone.CFrame = holder.CFrame:ToObjectSpace(world)
			bone.Parent = holder
		end
		made[index] = { bone = bone, world = world }
		names[index] = item.name
	end
	local motion = Instance.new("Folder") -- the actions' poses are kept here (see runMotion)
	motion.Name = "UnixMotion"
	motion:SetAttribute("Bones", HttpService:JSONEncode(names))
	motion.Parent = holder
	holder.Parent = entry.model
	entry.rigParts[name] = holder
	return holder
end

local function joinRig(part, item, entry, report)
	local holder = rigFor(entry, item.skin.rig)
	if not holder then
		table.insert(report.notes, "the skeleton of " .. item.name .. " did not arrive: it stands still")
		return
	end
	local weld = Instance.new("WeldConstraint")
	weld.Name = "UnixRigWeld"
	weld.Part0 = holder
	weld.Part1 = part
	weld.Parent = part
	local skinned = false
	pcall(function()
		skinned = part.HasSkinnedMesh
	end)
	if skinned then
		report.skinned = (report.skinned or 0) + 1
	else -- an uploaded mesh that came back without its skin
		report.unskinned = report.unskinned or {}
		table.insert(report.unskinned, item.name)
	end
end

-- an EditableMesh from the arrays Unix sent: positions, normals, optional UVs, triangles
local function fillMesh(item)
	local mesh = AssetService:CreateEditableMesh()
	local positions, normals, uvs, triangles = item.v, item.n, item.uv, item.t
	local count = #positions / 3
	local vertexIds = table.create(count)
	local normalIds = table.create(count)
	local uvIds = uvs and table.create(count) or nil
	for i = 0, count - 1 do
		vertexIds[i + 1] = mesh:AddVertex(Vector3.new(positions[i * 3 + 1], positions[i * 3 + 2], positions[i * 3 + 3]))
		normalIds[i + 1] = mesh:AddNormal(Vector3.new(normals[i * 3 + 1], normals[i * 3 + 2], normals[i * 3 + 3]))
		if uvIds then
			uvIds[i + 1] = mesh:AddUV(Vector2.new(uvs[i * 2 + 1], uvs[i * 2 + 2]))
		end
	end
	for i = 0, #triangles / 3 - 1 do
		local a, b, c = triangles[i * 3 + 1] + 1, triangles[i * 3 + 2] + 1, triangles[i * 3 + 3] + 1
		local face = mesh:AddTriangle(vertexIds[a], vertexIds[b], vertexIds[c])
		mesh:SetFaceNormals(face, { normalIds[a], normalIds[b], normalIds[c] })
		if uvIds then
			mesh:SetFaceUVs(face, { uvIds[a], uvIds[b], uvIds[c] })
		end
	end
	if item.skin then
		skinMesh(mesh, vertexIds, item.skin)
	end
	return mesh
end

-- Save an EditableMesh or EditableImage as an asset on the user's Roblox account (or the group
-- that owns the place). Only ever called when the user agreed to the upload in Unix.
local function uploadAsset(object, assetType, name)
	local params = { Name = string.sub(tostring(name), 1, 50) }
	pcall(function()
		if game.CreatorId ~= 0 and game.CreatorType == Enum.CreatorType.Group then
			params.CreatorId = game.CreatorId
			params.CreatorType = Enum.AssetCreatorType.Group
		end
	end)
	local ok, result, assetId = pcall(function()
		return AssetService:CreateAssetAsync(object, assetType, params)
	end)
	if ok and result == Enum.CreateAssetResult.Success and assetId then
		return assetId
	end
	return nil, tostring(result)
end

local function meshPartFrom(content)
	return AssetService:CreateMeshPartAsync(content, { CollisionFidelity = Enum.CollisionFidelity.Default })
end

-- an asset that was uploaded a moment ago can need a few seconds before it loads
local function meshPartFromAsset(uri, tries)
	for attempt = 1, tries do
		local ok, part = pcall(function()
			return meshPartFrom(Content.fromUri(uri))
		end)
		if ok and part then
			return part
		end
		if attempt < tries then
			task.wait(1.5)
		end
	end
	return nil
end

-- One shape, however many parts use it: built (and uploaded) the first time, named after that.
local function shapeFor(item, entry, upload, report)
	local key = item.key
	local shape = key and entry.shapes[key]
	if shape then
		return shape
	end
	shape = { source = item.v and item or nil }
	local kept = key and entry.keptMeshes[key]
	if kept then
		shape.uri = kept -- this place already uses an asset for exactly this shape
		report.reused = report.reused + 1
	else
		if not item.v then
			error("its shape was not sent")
		end
		local mesh = fillMesh(item)
		shape.content = Content.fromObject(mesh)
		if upload then
			local assetId, problem = uploadAsset(mesh, Enum.AssetType.Mesh, item.name)
			if assetId then
				shape.uri = "rbxassetid://" .. tostring(assetId)
				shape.assetId = assetId
				shape.fresh = true
				report.uploaded = report.uploaded + 1
			else
				table.insert(report.notes, "could not upload the mesh of " .. item.name .. ": " .. tostring(problem))
			end
		end
	end
	if key then
		entry.shapes[key] = shape
	end
	return shape
end

local function imageFor(item, entry, upload, report)
	local imageKey = item.texture.key
	local image = entry.images[imageKey]
	if image then
		return image
	end
	image = {}
	if entry.keptImages[imageKey] then
		image.uri = entry.keptImages[imageKey] -- this place already uses an asset for exactly this picture
	elseif item.texture.data then
		local size = Vector2.new(item.texture.w, item.texture.h)
		local pixels = AssetService:CreateEditableImage({ Size = size })
		pixels:WritePixelsBuffer(Vector2.zero, size, decodeBase64(item.texture.data))
		image.content = Content.fromObject(pixels)
		if upload then
			local assetId, failed = uploadAsset(pixels, Enum.AssetType.Image, item.name .. "_texture")
			if assetId then
				image.uri = "rbxassetid://" .. tostring(assetId)
				report.textures = (report.textures or 0) + 1
			else
				table.insert(report.notes, "could not upload the texture of " .. item.name .. ": " .. tostring(failed))
			end
		end
	end
	entry.images[imageKey] = image -- later pieces of the same material reuse it
	return image
end

-- Run fn(item) for every item, several at a time. Uploading and loading assets is waiting on
-- Roblox's servers, so doing them side by side is what makes a big send quick.
local function sideBySide(items, workers, fn)
	local nextIndex, running = 0, 0
	local function work()
		while true do
			nextIndex = nextIndex + 1
			local item = items[nextIndex]
			if item == nil then
				break
			end
			fn(item)
		end
		running = running - 1
	end
	for _ = 1, math.min(workers, #items) do
		running = running + 1
		task.spawn(work)
	end
	while running > 0 do
		task.wait(0.03)
	end
end

local surfaces = {} -- picture, apply, wanted, done: set below, once pictures can arrive

-- A mesh uploaded a moment ago loads only once Roblox has processed it, which takes minutes when a
-- whole world is uploaded at once ("Asset content representation is being generated"). The part is
-- built from memory meanwhile, so the send goes on; this keeps trying the upload and, once it loads,
-- puts it into every part waiting for it. Without it those parts stayed checkered boxes in Play.
local ADOPT_EVERY, ADOPT_FOR = 5, 600

local function adoptWhenReady(shape)
	if shape.adopting then
		return
	end
	shape.adopting = true
	task.spawn(function()
		local deadline = os.clock() + ADOPT_FOR
		while os.clock() < deadline do
			task.wait(ADOPT_EVERY)
			local ok, ready = pcall(function()
				return meshPartFrom(Content.fromUri(shape.uri))
			end)
			if ok and ready then
				local adopted = 0
				for _, waiting in ipairs(shape.waiting) do
					local part = waiting.part
					if part.Parent then
						local size, cframe = part.Size, part.CFrame
						if pcall(function()
							part:ApplyMesh(ready)
						end) then
							part.Size, part.CFrame = size, cframe
							part:SetAttribute("UnixMeshAsset", shape.assetId)
							if waiting.onlyMesh then
								part:SetAttribute("UnixPreview", nil)
							end
							adopted = adopted + 1
						end
					end
				end
				ready:Destroy()
				shape.broken, shape.waiting = false, {}
				print("[Unix Bridge] " .. adopted .. " part(s) now use their uploaded mesh " .. shape.uri)
				return
			end
		end
		shape.adopting = false
		warn("[Unix Bridge] the mesh asset " .. shape.uri .. " did not load in " .. ADOPT_FOR .. " s: send again to retry")
	end)
end

local function buildMesh(item, entry, upload, report)
	local shape = shapeFor(item, entry, upload, report)
	local part
	if shape.uri and not shape.broken and not shape.fresh then
		part = meshPartFromAsset(shape.uri, 1)
		if not part then
			shape.broken = true
			table.insert(report.notes, "the mesh asset for " .. item.name .. " (" .. shape.uri .. ") is not loading yet: "
				.. "Roblox is still processing the upload. The part shows its mesh from memory and takes the uploaded one by "
				.. "itself when it is ready (in a few minutes), while Studio stays open.")
		end
	end
	local meshKept = part ~= nil
	local picturesStay = true
	if not part then
		if not shape.content then
			if not shape.source then
				error("its shape was not sent")
			end
			shape.content = Content.fromObject(fillMesh(shape.source))
		end
		-- Measured in Studio: when a part with a SurfaceAppearance and a part without one share one
		-- mesh held in memory, one of the two is not drawn. So each kind gets a mesh of its own.
		local content = shape.content
		local kind = item.surface and "surfaced" or "plain"
		if shape.kind == nil then
			shape.kind = kind
		elseif shape.kind ~= kind and shape.source then
			if not shape.other then
				shape.other = Content.fromObject(fillMesh(shape.source))
			end
			content = shape.other
		end
		part = meshPartFrom(content)
	end
	part.Name = item.name
	part.Anchored = true
	part.Color = color3(item.color)
	part.Material = material(item.material)
	part.Transparency = item.transparency or 0
	part.Reflectance = item.reflectance or 0
	if item.double_sided then -- a sheet or an open shell: no face may go missing from behind
		pcall(function()
			part.DoubleSided = true
		end)
	end
	if item.size then -- a copy of a shape at another size
		local now, want = part.Size, item.size
		local size = Vector3.new(want[1] > 0.0005 and want[1] or now.X, want[2] > 0.0005 and want[2] or now.Y,
			want[3] > 0.0005 and want[3] or now.Z)
		if (size - now).Magnitude > 0.002 then
			part.Size = size
		end
	end
	local centre = item.center
	if item.rot then
		local r = item.rot
		part.CFrame = CFrame.new(centre[1], centre[2], centre[3], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]) + entry.origin
	else
		part.CFrame = CFrame.new(centre[1], centre[2], centre[3]) + entry.origin
	end
	if item.key then
		part:SetAttribute("UnixMesh", item.key)
	end
	if shape.assetId then
		part:SetAttribute("UnixMeshAsset", shape.assetId)
	end

	if item.texture then
		local okImage, problem = pcall(function()
			local image = imageFor(item, entry, upload, report)
			if image.uri then
				part.TextureID = image.uri
				part:SetAttribute("UnixTexture", item.texture.key)
				part.Color = Color3.new(1, 1, 1)
			elseif image.content then
				picturesStay = false -- the picture would be lost in Play even if the mesh is kept
				part.TextureContent = image.content
				part.Color = Color3.new(1, 1, 1)
			end
		end)
		if not okImage then
			table.insert(report.notes, "texture skipped on " .. item.name .. ": " .. tostring(problem))
		end
	end
	if item.surface then
		local okSurface, stays = pcall(surfaces.apply, part, item, entry, upload, report)
		if not okSurface then
			table.insert(report.notes, "surface skipped on " .. item.name .. ": " .. tostring(stays))
		elseif not stays then
			picturesStay = false -- its pictures would be lost in Play even if the mesh is kept
		end
	end

	local kept = meshKept and picturesStay
	if not meshKept and shape.uri then -- uploaded but not loading yet: swapped in when Roblox has it ready
		shape.waiting = shape.waiting or {}
		table.insert(shape.waiting, { part = part, onlyMesh = picturesStay })
		adoptWhenReady(shape)
		if picturesStay then
			report.adopting = (report.adopting or 0) + 1 -- in the game as soon as Roblox has processed the upload
		end
	end
	if kept then
		report.kept = report.kept + 1
	else
		-- built from this session's memory only: Roblox shows a checkered box for it in Play
		part:SetAttribute("UnixPreview", true)
		report.preview = report.preview + 1
	end
	if item.skin then
		joinRig(part, item, entry, report)
	end
	attachTrack(part, item.track, entry)
	part.Parent = entry.model
	report.meshes = report.meshes + 1
end

local function sequence(points)
	local keys = {}
	for _, p in ipairs(points) do
		table.insert(keys, NumberSequenceKeypoint.new(p[1], p[2]))
	end
	return NumberSequence.new(keys)
end

local function colors(points)
	local keys = {}
	for _, p in ipairs(points) do
		table.insert(keys, ColorSequenceKeypoint.new(p[1], Color3.new(p[2], p[3], p[4])))
	end
	return ColorSequence.new(keys)
end

local FIRE = "rbxasset://textures/particles/fire_main.dds"
local SMOKE = "rbxasset://textures/particles/smoke_main.dds"
local SPARK = "rbxasset://textures/particles/sparkles_main.dds"

local function emitter(parent, name, props)
	local e = Instance.new("ParticleEmitter")
	e.Name = name
	for key, value in pairs(props) do
		e[key] = value
	end
	e.Parent = parent
	return e
end

-- An effect's film: the frames Unix rendered of it, as one square picture of grid x grid cells.
-- It arrives a band of rows at a time (one job each) and is put together here. The pictures of
-- surfaces (colour, normal, roughness, metalness, glow) arrive the same way, ahead of their meshes.
local pictures = {} -- picture key -> EditableImage

-- The bytes of one band, as RGBA. A grey picture (a roughness or metal map, a mask) travels as one
-- byte a pixel and is widened here.
local function pictureBytes(job)
	local bytes
	local ok, decoded = pcall(function()
		return game:GetService("EncodingService"):Base64Decode(buffer.fromstring(job.data))
	end)
	if ok and typeof(decoded) == "buffer" then
		bytes = decoded -- Studio's own decoder: a megabyte in a hundredth of a second
	else
		bytes = decodeBase64(job.data)
	end
	if job.c ~= 1 then
		return bytes
	end
	local count = buffer.len(bytes)
	local wide = buffer.create(count * 4)
	for i = 0, count - 1 do
		buffer.writeu32(wide, i * 4, buffer.readu8(bytes, i) * 65793 + 4278190080) -- grey, grey, grey, opaque
	end
	return wide
end

local function runPicture(job)
	local picture = pictures[job.key]
	if not picture or job.y == 0 then
		picture = AssetService:CreateEditableImage({ Size = Vector2.new(job.w, job.h) })
		pictures[job.key] = picture
	end
	picture:WritePixelsBuffer(Vector2.new(0, job.y), Vector2.new(job.w, job.rows), pictureBytes(job))
	return { picture = job.key, rows = job.rows, notes = {}, place = placeName() }
end

-- One picture of a surface, by key: an image asset when the user agreed to the upload (or the
-- place already uses one for exactly these pixels), else the picture held in this session's memory.
surfaces.picture = function(key, entry, upload, report, name)
	local image = entry.images[key]
	if image then
		return image
	end
	image = {}
	entry.images[key] = image -- claimed before anything waits: one picture is never uploaded twice
	if entry.keptImages[key] then
		image.uri = entry.keptImages[key]
		report.pictures_reused = (report.pictures_reused or 0) + 1
	elseif pictures[key] then
		image.content = Content.fromObject(pictures[key])
		if upload then
			local assetId, failed = uploadAsset(pictures[key], Enum.AssetType.Image, name)
			if assetId then
				image.uri = "rbxassetid://" .. tostring(assetId)
				report.pictures_uploaded = (report.pictures_uploaded or 0) + 1
			else
				table.insert(report.notes, "could not upload the picture " .. tostring(name) .. ": " .. tostring(failed))
			end
		end
	end
	return image
end

-- The pictures these meshes name that this asset has not taken in yet, each once.
surfaces.wanted = function(meshes, entry)
	local list, seen = {}, {}
	for _, item in ipairs(meshes) do
		for role, key in pairs(item.surface and item.surface.maps or {}) do
			if not seen[key] and not entry.images[key] then
				seen[key] = true
				table.insert(list, { key = key, name = tostring(item.name) .. "_" .. tostring(role) })
			end
		end
	end
	return list
end

-- The whole look of a mesh: a SurfaceAppearance from the pictures Unix sent, with what their
-- alpha does, a tint and the strength of the light given off. Returns false when one of its
-- pictures is only in this session's memory: it shows while the place is edited and is gone in Play.
surfaces.apply = function(part, item, entry, upload, report)
	local surface = item.surface
	local look = Instance.new("SurfaceAppearance")
	local stays = true
	for role, key in pairs(surface.maps or {}) do
		local property = SURFACE_MAPS[role]
		local image = property and surfaces.picture(key, entry, upload, report, tostring(item.name) .. "_" .. role)
		local shown = false
		if image and (image.uri or image.content) then
			shown = pcall(function()
				look[property] = image.uri and Content.fromUri(image.uri) or image.content
			end)
		end
		if shown and image.uri then
			look:SetAttribute("Unix_" .. role, key)
		elseif shown then
			stays = false
		elseif property then
			table.insert(report.notes, "the " .. role .. " picture of " .. tostring(item.name) .. " could not be shown")
		end
	end
	pcall(function()
		if surface.alpha then
			look.AlphaMode = Enum.AlphaMode[surface.alpha]
		end
		if surface.tint then
			look.Color = color3(surface.tint)
		end
		if surface.emissive_strength then
			look.EmissiveStrength = surface.emissive_strength
		end
	end)
	look.Parent = part
	return stays
end

-- When an asset is complete its pictures are held by its parts (or are assets): the copies kept
-- here for building are let go.
surfaces.done = function(entry)
	for key in pairs(entry.images) do
		pictures[key] = nil
	end
end

local FLIPBOOKS = {
	[2] = Enum.ParticleFlipbookLayout.Grid2x2,
	[4] = Enum.ParticleFlipbookLayout.Grid4x4,
	[8] = Enum.ParticleFlipbookLayout.Grid8x8,
}

-- An effect that was made in Unix, shown as it was rendered there: one emitter plays the film as
-- a looping flipbook on a sprite the size of the effect, standing upright and facing the camera.
-- A particle lives three seconds and a new one starts every two, each fading in as the last fades
-- out, so the brightness is steady and nothing ever pops. Returns false if it could not be done
-- (the caller then builds the stand-in from stock particles).
local function buildFilm(item, entry, upload, report)
	local film = item.film
	local uri = entry.keptFilms[film.key] -- this place already holds exactly these pictures
	if uri then
		report.reused = report.reused + 1
	else
		local picture = pictures[film.key]
		if not picture then
			table.insert(report.notes, "the pictures of " .. item.name .. " did not arrive: it was built from stock particles")
			return false
		end
		if not upload then
			return false
		end
		local assetId, failed = uploadAsset(picture, Enum.AssetType.Image, item.name .. "_film")
		if not assetId then
			table.insert(report.notes, "could not upload the pictures of " .. item.name .. " (" .. tostring(failed)
				.. "): it was built from stock particles instead")
			return false
		end
		uri = "rbxassetid://" .. tostring(assetId)
		report.uploaded = report.uploaded + 1
	end
	pictures[film.key] = nil

	local holder = Instance.new("Part")
	holder.Name = item.name
	holder.Anchored = true
	holder.CanCollide = false
	holder.CanQuery = false
	holder.Transparency = 1
	holder.Size = Vector3.new(0.2, 0.2, 0.2)
	holder.CFrame = CFrame.new(film.centre[1], film.centre[2], film.centre[3]) + entry.origin
	local glows = film.glows == true
	local shown = emitter(holder, "Film", {
		Texture = uri,
		FlipbookLayout = FLIPBOOKS[film.grid] or Enum.ParticleFlipbookLayout.Grid8x8,
		FlipbookMode = Enum.ParticleFlipbookMode.Loop,
		FlipbookFramerate = NumberRange.new(film.fps),
		FlipbookStartRandom = true,
		Orientation = Enum.ParticleOrientation.FacingCameraWorldUp,
		Size = NumberSequence.new(film.size),
		Squash = NumberSequence.new(film.squash or 0), -- the cell is square, the effect is not: stretched back
		Transparency = sequence({ { 0, 1 }, { 0.333, 0 }, { 0.667, 0 }, { 1, 1 } }),
		Lifetime = NumberRange.new(3), Rate = 0.5,
		Speed = NumberRange.new(0), Rotation = NumberRange.new(0), RotSpeed = NumberRange.new(0),
		LockedToPart = true,
		LightEmission = glows and 0.6 or 0, LightInfluence = glows and 0 or 1,
	})
	shown:SetAttribute("UnixFilm", film.key)
	if glows then
		local glow = Instance.new("PointLight")
		glow.Color = item.color and color3(item.color) or Color3.new(1, 0.55, 0.2)
		glow.Range = math.min(60, film.size * 1.2 + 8)
		glow.Brightness = 2.2
		glow.Parent = holder
	end
	holder.Parent = entry.model
	pcall(function()
		shown:Emit(1) -- visible at once while editing; in the game the first one starts within two seconds
	end)
	report.effects = report.effects + 1
	report.films = (report.films or 0) + 1
	return true
end

-- effects are rebuilt with Roblox's own particle system, sized in studs (s)
local function buildEffect(item, entry, report, upload)
	if item.film then
		local ok, built = pcall(buildFilm, item, entry, upload, report)
		if ok and built then
			return
		elseif not ok then
			table.insert(report.notes, "the film of " .. tostring(item.name) .. " failed (" .. tostring(built)
				.. "): it was built from stock particles instead")
		end
	end
	local s = item.size
	local tint = item.color
	local holder = Instance.new("Part")
	holder.Name = item.name
	holder.Anchored = true
	holder.CanCollide = false
	holder.CanQuery = false
	holder.Transparency = 1
	holder.Size = Vector3.new(s * 0.5, 0.2, s * 0.5)
	holder.CFrame = CFrame.new(item.position[1], item.position[2], item.position[3]) + entry.origin
	local effect = item.effect
	local up = Enum.NormalId.Top

	local function light(color, range, brightness)
		local glow = Instance.new("PointLight")
		glow.Color = color
		glow.Range = range
		glow.Brightness = brightness
		glow.Parent = holder
	end

	if effect == "fire" or effect == "explosion" then
		local hot = tint and color3(tint) or Color3.new(1, 0.55, 0.12)
		local burst = effect == "explosion"
		emitter(holder, "Flames", {
			Texture = FIRE, EmissionDirection = up, LightEmission = 0.85, LightInfluence = 0,
			Color = colors({ { 0, 1, 0.85, 0.45 }, { 0.35, hot.R, hot.G, hot.B }, { 1, 0.5, 0.06, 0.02 } }),
			Size = sequence({ { 0, s * (burst and 0.9 or 0.45) }, { 0.4, s * (burst and 1.6 or 0.6) }, { 1, s * 0.15 } }),
			Transparency = sequence({ { 0, 0.35 }, { 0.6, 0.55 }, { 1, 1 } }),
			Lifetime = NumberRange.new(burst and 0.5 or 0.7, burst and 0.9 or 1.3),
			Rate = burst and 90 or 55,
			Speed = NumberRange.new(s * (burst and 3 or 1.6), s * (burst and 5 or 2.6)),
			SpreadAngle = burst and Vector2.new(180, 180) or Vector2.new(12, 12),
			Acceleration = Vector3.new(0, s * 1.2, 0),
			Rotation = NumberRange.new(0, 360), RotSpeed = NumberRange.new(-70, 70),
		})
		emitter(holder, "Smoke", {
			Texture = SMOKE, EmissionDirection = up, LightEmission = 0, LightInfluence = 1,
			Color = colors({ { 0, 0.16, 0.15, 0.14 }, { 1, 0.3, 0.3, 0.3 } }),
			Size = sequence({ { 0, s * 0.4 }, { 1, s * 1.6 } }),
			Transparency = sequence({ { 0, 1 }, { 0.2, 0.7 }, { 1, 1 } }),
			Lifetime = NumberRange.new(2, 3.2), Rate = 9,
			Speed = NumberRange.new(s * 1.2, s * 2), SpreadAngle = Vector2.new(14, 14),
			Rotation = NumberRange.new(0, 360), RotSpeed = NumberRange.new(-25, 25),
		})
		emitter(holder, "Embers", {
			Texture = SPARK, EmissionDirection = up, LightEmission = 1, LightInfluence = 0,
			Color = colors({ { 0, 1, 0.8, 0.35 }, { 1, 1, 0.3, 0.05 } }),
			Size = sequence({ { 0, s * 0.07 }, { 1, 0 } }),
			Lifetime = NumberRange.new(1, 2.2), Rate = 14,
			Speed = NumberRange.new(s * 2, s * 4), SpreadAngle = Vector2.new(28, 28),
			Acceleration = Vector3.new(0, s * 0.8, 0), Drag = 0.6,
		})
		light(Color3.new(1, 0.55, 0.2), s * 9 + 8, 2.2)
	elseif effect == "smoke" then
		local grey = tint and color3(tint) or Color3.new(0.3, 0.3, 0.32)
		emitter(holder, "Smoke", {
			Texture = SMOKE, EmissionDirection = up, LightEmission = 0, LightInfluence = 1,
			Color = ColorSequence.new(grey),
			Size = sequence({ { 0, s * 0.5 }, { 1, s * 2.2 } }),
			Transparency = sequence({ { 0, 1 }, { 0.15, 0.55 }, { 1, 1 } }),
			Lifetime = NumberRange.new(3, 5), Rate = 12,
			Speed = NumberRange.new(s * 0.9, s * 1.6), SpreadAngle = Vector2.new(16, 16),
			Rotation = NumberRange.new(0, 360), RotSpeed = NumberRange.new(-20, 20),
		})
	elseif effect == "sparks" or effect == "dust_burst" then
		local sparks = effect == "sparks"
		local base = tint and color3(tint) or (sparks and Color3.new(1, 0.75, 0.3) or Color3.new(0.62, 0.55, 0.46))
		emitter(holder, sparks and "Sparks" or "Dust", {
			Texture = sparks and SPARK or SMOKE, EmissionDirection = up,
			LightEmission = sparks and 1 or 0, LightInfluence = sparks and 0 or 1,
			Color = ColorSequence.new(base),
			Size = sequence({ { 0, s * (sparks and 0.1 or 0.5) }, { 1, s * (sparks and 0 or 1.4) } }),
			Transparency = sequence({ { 0, sparks and 0 or 0.5 }, { 1, 1 } }),
			Lifetime = NumberRange.new(sparks and 0.6 or 1.2, sparks and 1.4 or 2),
			Rate = sparks and 40 or 14,
			Speed = NumberRange.new(s * (sparks and 4 or 0.8), s * (sparks and 8 or 1.6)),
			SpreadAngle = Vector2.new(sparks and 55 or 80, sparks and 55 or 80),
			Acceleration = Vector3.new(0, sparks and -s * 9 or 0, 0), Drag = sparks and 0.5 or 2,
		})
		if sparks then
			light(base, s * 6 + 6, 1.5)
		end
	elseif effect == "rain" or effect == "snow" then
		local rain = effect == "rain"
		holder.Size = Vector3.new(s * 4, 0.2, s * 4)
		holder.CFrame = holder.CFrame + Vector3.new(0, s * 5, 0)
		emitter(holder, rain and "Rain" or "Snow", {
			Texture = SPARK, EmissionDirection = Enum.NormalId.Bottom, LightEmission = 0.3,
			Color = ColorSequence.new(tint and color3(tint) or (rain and Color3.new(0.7, 0.8, 1) or Color3.new(1, 1, 1))),
			Size = sequence({ { 0, rain and 0.12 or 0.22 }, { 1, rain and 0.12 or 0.22 } }),
			Squash = sequence({ { 0, rain and 3 or 0 }, { 1, rain and 3 or 0 } }),
			Transparency = sequence({ { 0, 0.3 }, { 1, 0.6 } }),
			Lifetime = NumberRange.new(rain and 0.5 or 3, rain and 0.8 or 5),
			Rate = rain and 500 or 160,
			Speed = NumberRange.new(rain and s * 9 or s * 1, rain and s * 12 or s * 1.8),
			SpreadAngle = Vector2.new(rain and 2 or 18, rain and 2 or 18),
		})
	else
		-- magic_swirl, shockwave, energy_shield: a glowing shell and orbiting sparkles
		local glow = tint and color3(tint) or Color3.new(0.5, 0.35, 1)
		local shell = Instance.new("Part")
		shell.Name = "Glow"
		shell.Shape = Enum.PartType.Ball
		shell.Anchored = true
		shell.CanCollide = false
		shell.Material = Enum.Material.ForceField
		shell.Color = glow
		shell.Size = Vector3.new(s, s, s)
		shell.CFrame = holder.CFrame + Vector3.new(0, s * 0.5, 0)
		shell.Parent = entry.model
		emitter(holder, "Sparkles", {
			Texture = SPARK, EmissionDirection = up, LightEmission = 1, LightInfluence = 0,
			Color = ColorSequence.new(glow),
			Size = sequence({ { 0, s * 0.12 }, { 1, 0 } }),
			Lifetime = NumberRange.new(1, 1.8), Rate = 30,
			Speed = NumberRange.new(s * 0.6, s * 1.2), SpreadAngle = Vector2.new(180, 180),
			RotSpeed = NumberRange.new(-120, 120),
		})
		light(glow, s * 6 + 6, 1.6)
	end

	holder.Parent = entry.model
	report.effects = report.effects + 1
end

---------------------------------------------------------------------------------------------
-- animation described as data: attributes on parts, lights and beams
--   Spin   (Vector3, degrees per second about the part's own X, Y, Z)
--   Pulse  (number, e.g. 0.05 = size or brightness swells by 5%), PulseSpeed (default 2.4)
--   Flicker (number, studs: a Beam's curve is re-rolled within +-Flicker twenty times a second)
-- The same few lines run here, so the effect moves while you edit, and in the game (ANIMATOR).

local ANIMATOR = [==[
-- Unix animator: plays the Spin, Pulse and Flicker attributes of this model.
-- Stop:  model:SetAttribute("Enabled", false)      Start:  model:SetAttribute("Enabled", true)
-- Remove the effect by destroying the model; this script cleans up after itself.
local RunService = game:GetService("RunService")
local HttpService = game:GetService("HttpService")
local model = script.Parent
local random = Random.new()

local function collect(root)
	local entries = {}
	for _, item in ipairs(root:GetDescendants()) do
		if item:IsA("StringValue") and item.Name == "UnixTrack" and item.Parent and item.Parent:IsA("BasePart") then
			local ok, data = pcall(function()
				return HttpService:JSONDecode(item.Value)
			end)
			if ok and data and data.frames and #data.frames > 1 then
				local frames = {}
				for index, f in ipairs(data.frames) do
					frames[index] = CFrame.new(f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], f[9], f[10], f[11], f[12])
				end
				table.insert(entries, { item = item.Parent, wait = 0, frames = frames, fps = data.fps or 24 })
			end
		end
		local spin, pulse, flicker = item:GetAttribute("Spin"), item:GetAttribute("Pulse"), item:GetAttribute("Flicker")
		if spin or pulse or flicker then
			local entry = { item = item, wait = 0 }
			if spin and item:IsA("BasePart") then
				entry.spin = spin
			end
			if pulse then
				entry.pulse = pulse
				entry.pulseSpeed = item:GetAttribute("PulseSpeed") or 2.4
				if item:IsA("BasePart") then
					entry.baseSize = item:GetAttribute("BaseSize") or item.Size
				elseif item:IsA("Light") then
					entry.baseBrightness = item:GetAttribute("BaseBrightness") or item.Brightness
				end
			end
			if flicker and item:IsA("Beam") then
				entry.flicker = flicker
			end
			table.insert(entries, entry)
		end
	end
	return entries
end

local function step(entries, dt, now)
	for _, entry in ipairs(entries) do
		local item = entry.item
		if item.Parent then
			if entry.frames then      -- motion sampled from Blender's timeline, looping
				local position = now * entry.fps
				local count = #entry.frames
				local index = math.floor(position) % count
				item.CFrame = entry.frames[index + 1]:Lerp(entry.frames[(index + 1) % count + 1], position - math.floor(position))
			end
			if entry.spin then
				local spin = entry.spin
				item.CFrame = item.CFrame * CFrame.Angles(math.rad(spin.X) * dt, math.rad(spin.Y) * dt, math.rad(spin.Z) * dt)
			end
			if entry.pulse then
				local wave = math.sin(now * entry.pulseSpeed)
				if entry.baseSize then
					item.Size = entry.baseSize * (1 + entry.pulse * wave)
				elseif entry.baseBrightness then
					item.Brightness = entry.baseBrightness * (1 + entry.pulse * 4 * wave)
				end
			end
			if entry.flicker then
				entry.wait = entry.wait - dt
				if entry.wait <= 0 then
					entry.wait = 0.05
					item.CurveSize0 = random:NextNumber(-entry.flicker, entry.flicker)
					item.CurveSize1 = random:NextNumber(-entry.flicker, entry.flicker)
					item.Enabled = random:NextNumber() > 0.2
				end
			end
		end
	end
end

local entries = collect(model)
local beat = RunService.Heartbeat:Connect(function(dt)
	if model:GetAttribute("Enabled") ~= false then
		step(entries, dt, os.clock())
	end
end)
local switch = model:GetAttributeChangedSignal("Enabled"):Connect(function()
	local on = model:GetAttribute("Enabled") ~= false
	for _, item in ipairs(model:GetDescendants()) do
		if item:IsA("ParticleEmitter") or item:IsA("Beam") or item:IsA("Light") or item:IsA("Trail") then
			item.Enabled = on
		end
	end
end)
model.Destroying:Connect(function()
	beat:Disconnect()
	switch:Disconnect()
end)
]==]

local previewOn = true
local previewRandom = Random.new()
local previews = {} -- root -> entries

local function collectAnimated(root)
	local entries = {}
	for _, item in ipairs(root:GetDescendants()) do
		if item:IsA("StringValue") and item.Name == "UnixTrack" and item.Parent and item.Parent:IsA("BasePart") then
			local ok, data = pcall(function()
				return HttpService:JSONDecode(item.Value)
			end)
			if ok and data and data.frames and #data.frames > 1 then
				local frames = {}
				for index, f in ipairs(data.frames) do
					frames[index] = CFrame.new(f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], f[9], f[10], f[11], f[12])
				end
				table.insert(entries, { item = item.Parent, wait = 0, frames = frames, fps = data.fps or 24 })
			end
		end
		local spin, pulse, flicker = item:GetAttribute("Spin"), item:GetAttribute("Pulse"), item:GetAttribute("Flicker")
		if spin or pulse or flicker then
			local entry = { item = item, wait = 0 }
			if spin and item:IsA("BasePart") then
				entry.spin = spin
			end
			if pulse then
				entry.pulse = pulse
				entry.pulseSpeed = item:GetAttribute("PulseSpeed") or 2.4
				if item:IsA("BasePart") then
					entry.baseSize = item:GetAttribute("BaseSize") or item.Size
				elseif item:IsA("Light") then
					entry.baseBrightness = item:GetAttribute("BaseBrightness") or item.Brightness
				end
			end
			if flicker and item:IsA("Beam") then
				entry.flicker = flicker
			end
			table.insert(entries, entry)
		end
	end
	return entries
end

local function stepAnimated(entries, dt, now)
	for _, entry in ipairs(entries) do
		local item = entry.item
		if item.Parent then
			if entry.frames then      -- motion sampled from Blender's timeline, looping
				local position = now * entry.fps
				local count = #entry.frames
				local index = math.floor(position) % count
				item.CFrame = entry.frames[index + 1]:Lerp(entry.frames[(index + 1) % count + 1], position - math.floor(position))
			end
			if entry.spin then
				local spin = entry.spin
				item.CFrame = item.CFrame * CFrame.Angles(math.rad(spin.X) * dt, math.rad(spin.Y) * dt, math.rad(spin.Z) * dt)
			end
			if entry.pulse then
				local wave = math.sin(now * entry.pulseSpeed)
				if entry.baseSize then
					item.Size = entry.baseSize * (1 + entry.pulse * wave)
				elseif entry.baseBrightness then
					item.Brightness = entry.baseBrightness * (1 + entry.pulse * 4 * wave)
				end
			end
			if entry.flicker then
				entry.wait = entry.wait - dt
				if entry.wait <= 0 then
					entry.wait = 0.05
					item.CurveSize0 = previewRandom:NextNumber(-entry.flicker, entry.flicker)
					item.CurveSize1 = previewRandom:NextNumber(-entry.flicker, entry.flicker)
					item.Enabled = previewRandom:NextNumber() > 0.2
				end
			end
		end
	end
end

local function watch(root)
	local entries = collectAnimated(root)
	if #entries > 0 then
		previews[root] = entries
	end
	return #entries
end

for _, item in ipairs(Workspace:GetDescendants()) do
	if item:GetAttribute("UnixAnimated") == true then
		watch(item)
	end
end

RunService.Heartbeat:Connect(function(dt)
	if not previewOn or not current() then
		return
	end
	local now = os.clock()
	for root, entries in pairs(previews) do
		if root.Parent == nil then
			previews[root] = nil
		elseif root:GetAttribute("Enabled") ~= false then
			stepAnimated(entries, dt, now)
		end
	end
end)

togglePreview = function()
	previewOn = not previewOn
	print("[Unix Bridge] edit-mode animation preview " .. (previewOn and "on" or "off"))
end

---------------------------------------------------------------------------------------------
-- rig animation: the actions of an armature, sampled in Blender, as poses for its Bone instances.
-- Model attributes steer it: Animation (an action's name), Enabled, AnimationSpeed.
-- In the game a script in the model plays them (RIG_ANIMATOR, fixed text: the poses are data).

local RIG_ANIMATOR = [==[
-- Unix rig animator: plays the actions that came from Blender on the bones of this model.
-- It runs on each player's own screen, so no animation asset is needed and nothing is sent.
-- Choose:  model:SetAttribute("Animation", "Walk")       (the Animations attribute lists the names)
-- Stop:    model:SetAttribute("Enabled", false)           Start:  model:SetAttribute("Enabled", true)
-- Speed:   model:SetAttribute("AnimationSpeed", 0.5)
local RunService = game:GetService("RunService")
local HttpService = game:GetService("HttpService")
local model = script.Parent

local B64 = {}
do
	local alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
	for i = 1, #alphabet do
		B64[string.byte(alphabet, i)] = i - 1
	end
end

-- base64 text -> buffer of bytes
local function bytesOf(text)
	local ok, data = pcall(function()
		return HttpService:JSONDecode('{"m":null,"t":"buffer","base64":"' .. text .. '"}')
	end)
	if ok and typeof(data) == "buffer" then
		return data
	end
	local length = #text
	local padding = (string.sub(text, -2) == "==" and 2) or (string.sub(text, -1) == "=" and 1) or 0
	local size = (length / 4) * 3 - padding
	local out = buffer.create(size)
	local position = 0
	for i = 1, length, 4 do
		local a, b, c, d = string.byte(text, i, i + 3)
		local value = (B64[a] or 0) * 262144 + (B64[b] or 0) * 4096 + (B64[c] or 0) * 64 + (B64[d] or 0)
		if position < size then
			buffer.writeu8(out, position, math.floor(value / 65536) % 256)
		end
		if position + 1 < size then
			buffer.writeu8(out, position + 1, math.floor(value / 256) % 256)
		end
		if position + 2 < size then
			buffer.writeu8(out, position + 2, value % 256)
		end
		position = position + 3
	end
	return out
end

-- the actions of one skeleton: UnixMotion holds a folder per action, its poses as base64 text
local function loadRig(holder)
	local motion = holder:FindFirstChild("UnixMotion")
	local ok, names = pcall(function()
		return HttpService:JSONDecode(motion:GetAttribute("Bones"))
	end)
	if not ok or type(names) ~= "table" then
		return nil
	end
	local byName = {}
	for _, item in ipairs(holder:GetDescendants()) do
		if item:IsA("Bone") then
			byName[item.Name] = item
		end
	end
	local bones = table.create(#names)
	for index, name in ipairs(names) do
		bones[index] = byName[name] or false
	end
	local rig = { holder = holder, bones = bones, actions = {}, clock = 0 }
	for _, folder in ipairs(motion:GetChildren()) do
		local texts = {}
		for index = 1, #folder:GetChildren() do
			local value = folder:FindFirstChild(tostring(index))
			texts[index] = value and value:IsA("StringValue") and value.Value or ""
		end
		local frames, fps = folder:GetAttribute("Frames"), folder:GetAttribute("Fps")
		local read, data, moving = pcall(function()
			return bytesOf(table.concat(texts)), HttpService:JSONDecode(folder:GetAttribute("Moving") or "[]")
		end)
		if read and type(frames) == "number" and frames >= 1 then
			local slots = {} -- bone -> where its offset is, for the bones that shift
			for slot, bone in ipairs(moving) do
				slots[bone + 1] = slot - 1
			end
			local stride = #names * 8 + #moving * 12
			if buffer.len(data) >= frames * stride then
				rig.actions[folder.Name] = { data = data, frames = frames, fps = fps or 24, slots = slots, stride = stride,
					loop = folder:GetAttribute("Loop") == true }
			end
		end
	end
	return rig
end

-- one bone's pose (its Transform) between two keys, which start at bytes a and b of the action
local function poseOf(action, count, index, a, b, mix)
	local data, at = action.data, (index - 1) * 8
	local x, y = buffer.readi16(data, a + at), buffer.readi16(data, a + at + 2)
	local z, w = buffer.readi16(data, a + at + 4), buffer.readi16(data, a + at + 6)
	local slot = action.slots[index]
	local p = slot and count * 8 + slot * 12
	local px, py, pz = 0, 0, 0
	if p then
		px, py, pz = buffer.readf32(data, a + p), buffer.readf32(data, a + p + 4), buffer.readf32(data, a + p + 8)
	end
	if mix > 0 then
		x = x + (buffer.readi16(data, b + at) - x) * mix
		y = y + (buffer.readi16(data, b + at + 2) - y) * mix
		z = z + (buffer.readi16(data, b + at + 4) - z) * mix
		w = w + (buffer.readi16(data, b + at + 6) - w) * mix
		if p then
			px = px + (buffer.readf32(data, b + p) - px) * mix
			py = py + (buffer.readf32(data, b + p + 4) - py) * mix
			pz = pz + (buffer.readf32(data, b + p + 8) - pz) * mix
		end
	end
	local length = math.sqrt(x * x + y * y + z * z + w * w)
	if length == 0 then
		return CFrame.new(px, py, pz)
	end
	return CFrame.new(px, py, pz, x / length, y / length, z / length, w / length)
end

-- move one skeleton on by dt seconds of the action its model asks for, looping
local function stepRig(rig, model, dt)
	local wanted = rig.holder:GetAttribute("Animation") or model:GetAttribute("Animation")
	local action = (wanted and rig.actions[wanted]) or rig.actions[rig.holder:GetAttribute("UnixDefault") or ""]
	if not action then
		return
	end
	if rig.playing ~= action then
		rig.playing, rig.clock = action, 0
	end
	local speed = model:GetAttribute("AnimationSpeed")
	rig.clock = rig.clock + dt * (type(speed) == "number" and speed or 1)
	local last = action.frames - 1
	local position = last > 0 and (rig.clock * action.fps) % last or 0
	local first = math.min(math.floor(position), last)
	local a = first * action.stride
	local b = first < last and a + action.stride or a
	local count = #rig.bones
	for index, bone in ipairs(rig.bones) do
		if bone then
			bone.Transform = poseOf(action, count, index, a, b, position - first)
		end
	end
end

local rigs = {}
local stale = nil

local function collect()
	rigs = {}
	for _, item in ipairs(model:GetDescendants()) do
		if item:IsA("BasePart") and item:GetAttribute("UnixRig") then
			local rig = loadRig(item)
			if rig then
				table.insert(rigs, rig)
			end
		end
	end
end

collect()
-- parts of the model can arrive after this script does: look again when its contents change
local arrived = model.DescendantAdded:Connect(function()
	stale = os.clock()
end)
local beat = RunService.Heartbeat:Connect(function(dt)
	if stale and os.clock() - stale > 0.3 then
		stale = nil
		collect()
	end
	if model:GetAttribute("Enabled") ~= false then
		for _, rig in ipairs(rigs) do
			stepRig(rig, model, dt)
		end
	end
end)
model.Destroying:Connect(function()
	beat:Disconnect()
	arrived:Disconnect()
end)
]==]

-- The same few functions run here, so a rig moves while you edit (as with ANIMATOR above).
local function bytesOf(text)
	local ok, data = pcall(function()
		return HttpService:JSONDecode('{"m":null,"t":"buffer","base64":"' .. text .. '"}')
	end)
	if ok and typeof(data) == "buffer" then
		return data
	end
	return decodeBase64(text)
end

-- the actions of one skeleton: UnixMotion holds a folder per action, its poses as base64 text
local function loadRig(holder)
	local motion = holder:FindFirstChild("UnixMotion")
	local ok, names = pcall(function()
		return HttpService:JSONDecode(motion:GetAttribute("Bones"))
	end)
	if not ok or type(names) ~= "table" then
		return nil
	end
	local byName = {}
	for _, item in ipairs(holder:GetDescendants()) do
		if item:IsA("Bone") then
			byName[item.Name] = item
		end
	end
	local bones = table.create(#names)
	for index, name in ipairs(names) do
		bones[index] = byName[name] or false
	end
	local rig = { holder = holder, bones = bones, actions = {}, clock = 0 }
	for _, folder in ipairs(motion:GetChildren()) do
		local texts = {}
		for index = 1, #folder:GetChildren() do
			local value = folder:FindFirstChild(tostring(index))
			texts[index] = value and value:IsA("StringValue") and value.Value or ""
		end
		local frames, fps = folder:GetAttribute("Frames"), folder:GetAttribute("Fps")
		local read, data, moving = pcall(function()
			return bytesOf(table.concat(texts)), HttpService:JSONDecode(folder:GetAttribute("Moving") or "[]")
		end)
		if read and type(frames) == "number" and frames >= 1 then
			local slots = {} -- bone -> where its offset is, for the bones that shift
			for slot, bone in ipairs(moving) do
				slots[bone + 1] = slot - 1
			end
			local stride = #names * 8 + #moving * 12
			if buffer.len(data) >= frames * stride then
				rig.actions[folder.Name] = { data = data, frames = frames, fps = fps or 24, slots = slots, stride = stride,
					loop = folder:GetAttribute("Loop") == true }
			end
		end
	end
	return rig
end

-- one bone's pose (its Transform) between two keys, which start at bytes a and b of the action
local function poseOf(action, count, index, a, b, mix)
	local data, at = action.data, (index - 1) * 8
	local x, y = buffer.readi16(data, a + at), buffer.readi16(data, a + at + 2)
	local z, w = buffer.readi16(data, a + at + 4), buffer.readi16(data, a + at + 6)
	local slot = action.slots[index]
	local p = slot and count * 8 + slot * 12
	local px, py, pz = 0, 0, 0
	if p then
		px, py, pz = buffer.readf32(data, a + p), buffer.readf32(data, a + p + 4), buffer.readf32(data, a + p + 8)
	end
	if mix > 0 then
		x = x + (buffer.readi16(data, b + at) - x) * mix
		y = y + (buffer.readi16(data, b + at + 2) - y) * mix
		z = z + (buffer.readi16(data, b + at + 4) - z) * mix
		w = w + (buffer.readi16(data, b + at + 6) - w) * mix
		if p then
			px = px + (buffer.readf32(data, b + p) - px) * mix
			py = py + (buffer.readf32(data, b + p + 4) - py) * mix
			pz = pz + (buffer.readf32(data, b + p + 8) - pz) * mix
		end
	end
	local length = math.sqrt(x * x + y * y + z * z + w * w)
	if length == 0 then
		return CFrame.new(px, py, pz)
	end
	return CFrame.new(px, py, pz, x / length, y / length, z / length, w / length)
end

-- move one skeleton on by dt seconds of the action its model asks for, looping
local function stepRig(rig, model, dt)
	local wanted = rig.holder:GetAttribute("Animation") or model:GetAttribute("Animation")
	local action = (wanted and rig.actions[wanted]) or rig.actions[rig.holder:GetAttribute("UnixDefault") or ""]
	if not action then
		return
	end
	if rig.playing ~= action then
		rig.playing, rig.clock = action, 0
	end
	local speed = model:GetAttribute("AnimationSpeed")
	rig.clock = rig.clock + dt * (type(speed) == "number" and speed or 1)
	local last = action.frames - 1
	local position = last > 0 and (rig.clock * action.fps) % last or 0
	local first = math.min(math.floor(position), last)
	local a = first * action.stride
	local b = first < last and a + action.stride or a
	local count = #rig.bones
	for index, bone in ipairs(rig.bones) do
		if bone then
			bone.Transform = poseOf(action, count, index, a, b, position - first)
		end
	end
end

local rigged = {} -- model -> its skeletons, played while editing

local function watchRigs(model)
	local rigs = {}
	for _, item in ipairs(model:GetDescendants()) do
		if item:IsA("BasePart") and item:GetAttribute("UnixRig") then
			local rig = loadRig(item)
			if rig then
				table.insert(rigs, rig)
			end
		end
	end
	rigged[model] = #rigs > 0 and rigs or nil
	return rigs
end

for _, item in ipairs(Workspace:GetDescendants()) do
	if item:GetAttribute("UnixRigged") == true then
		pcall(watchRigs, item)
	end
end

RunService.Heartbeat:Connect(function(dt)
	if not previewOn or not current() then
		return
	end
	for model, rigs in pairs(rigged) do
		if model.Parent == nil then
			rigged[model] = nil
		elseif model:GetAttribute("Enabled") ~= false then
			for _, rig in ipairs(rigs) do
				stepRig(rig, model, dt)
			end
		end
	end
end)

-- The same poses as KeyframeSequences in the model's AnimSaves, where Roblox's Animation Editor
-- looks for them: from there the user can publish an action as an Animation asset himself.
-- Every thin-th key is written (a Pose is an instance, and a long action would be very many).
local function writeKeyframes(model, rig, thin)
	local saves = model:FindFirstChild("AnimSaves")
	if not saves then
		saves = Instance.new("Model")
		saves.Name = "AnimSaves"
		saves.Parent = model
	end
	local names = {}
	for name, action in pairs(rig.actions) do
		if action.frames > 1 then
			table.insert(names, name)
		end
	end
	table.sort(names)
	local count, written = #rig.bones, 0
	for _, name in ipairs(names) do
		local action = rig.actions[name]
		local sequence = Instance.new("KeyframeSequence")
		sequence.Name = saves:FindFirstChild(name) and (rig.holder.Name .. "_" .. name) or name
		sequence.Loop = action.loop
		sequence.Priority = Enum.AnimationPriority.Action
		local last, frame = action.frames - 1, 0
		while true do
			local key = Instance.new("Keyframe")
			key.Time = frame / action.fps
			local root = Instance.new("Pose") -- the part the bones hang on: it does not move
			root.Name = rig.holder.Name
			root.Weight = 0
			local poses = {}
			for index, bone in ipairs(rig.bones) do -- parents come before their children
				if bone then
					local pose = Instance.new("Pose")
					pose.Name = bone.Name
					pose.CFrame = poseOf(action, count, index, frame * action.stride, frame * action.stride, 0)
					pose.Parent = poses[bone.Parent] or root
					poses[bone] = pose
				end
			end
			root.Parent = key
			key.Parent = sequence
			if frame >= last then
				break
			end
			frame = math.min(frame + thin, last)
		end
		sequence.Parent = saves
		written = written + 1
		task.wait()
	end
	return written
end

-- An action's poses arrive as text, a few StringValues per job; the last job of a rig starts it.
local function runMotion(job)
	local entry = models[job.asset]
	if not entry or not entry.model.Parent then
		error("the model " .. tostring(job.asset) .. " is no longer there")
	end
	local holder = rigFor(entry, job.rig)
	if not holder then
		error("the skeleton " .. tostring(job.rig) .. " did not arrive")
	end
	local model = entry.model
	local report = { notes = {}, place = placeName(), model = model:GetFullName() }
	if job.values then
		local motion = holder.UnixMotion
		local folder = motion:FindFirstChild(job.name)
		if job.at == 0 or not folder then
			if folder then
				folder:Destroy()
			end
			folder = Instance.new("Folder")
			folder.Name = job.name
			folder:SetAttribute("Fps", job.fps)
			folder:SetAttribute("Frames", job.frames)
			folder:SetAttribute("Moving", HttpService:JSONEncode(job.moving or {}))
			folder:SetAttribute("Loop", job.loop == true)
			folder.Parent = motion
		end
		for index, text in ipairs(job.values) do
			local value = Instance.new("StringValue")
			value.Name = tostring(job.at + index)
			value.Value = text
			value.Parent = folder
		end
		report.motion = job.name
	end
	if job.done then
		holder:SetAttribute("UnixDefault", job.default or "")
		local rig = loadRig(holder)
		local names = {}
		for name in pairs(rig and rig.actions or {}) do
			table.insert(names, name)
		end
		table.sort(names)
		local known = model:GetAttribute("Animations")
		model:SetAttribute("Animations", (known and known ~= "" and (known .. ", ") or "") .. table.concat(names, ", "))
		if model:GetAttribute("Animation") == nil then
			model:SetAttribute("Animation", job.default or "")
		end
		model:SetAttribute("UnixRigged", true)
		pcall(function() -- with streaming on, the skeleton and its meshes must come and go together
			model.ModelStreamingMode = Enum.ModelStreamingMode.Atomic
		end)
		if not model:FindFirstChild("UnixRigAnimator") then
			local animator = Instance.new("Script")
			animator.Name = "UnixRigAnimator"
			-- Bone.Transform does not travel from the server to the players: it runs on each player's side
			animator.RunContext = Enum.RunContext.Client
			local written = pcall(function()
				animator.Source = RIG_ANIMATOR
			end)
			if written then
				animator.Parent = model
			else
				report.script = false
				table.insert(report.notes, "Studio blocked writing the rig animator script: the animation plays in Edit mode only. Allow script injection for Unix Bridge (Plugins > Manage Plugins).")
			end
		end
		if rig then
			local ok, written = pcall(writeKeyframes, model, rig, math.max(1, job.thin or 1))
			if ok then
				report.keyframes = written
			else
				table.insert(report.notes, "the KeyframeSequences of " .. tostring(job.rig) .. " were not written: " .. tostring(written))
			end
		end
		watchRigs(model)
		report.rig_ready = job.rig
		report.actions = #names
	end
	return report
end

---------------------------------------------------------------------------------------------
-- generic builder: any instance tree described as data

local function resolve(path)
	local names = string.split(path, ".")
	local first = names[1]
	if first == "game" then
		table.remove(names, 1)
		first = names[1]
	end
	local ok, current = pcall(function()
		return game:GetService(first)
	end)
	if not ok or not current then
		return nil
	end
	for i = 2, #names do
		current = current:FindFirstChild(names[i])
		if not current then
			return nil
		end
	end
	return current
end

local function numberSequence(points)
	if type(points) == "number" then
		return NumberSequence.new(points)
	end
	local keys = {}
	for _, point in ipairs(points) do
		table.insert(keys, NumberSequenceKeypoint.new(point[1], point[2], point[3] or 0))
	end
	return NumberSequence.new(keys)
end

local function colorSequence(points)
	if type(points[1]) == "number" then
		return ColorSequence.new(Color3.new(points[1], points[2], points[3]))
	end
	local keys = {}
	for _, point in ipairs(points) do
		table.insert(keys, ColorSequenceKeypoint.new(point[1], Color3.new(point[2], point[3], point[4])))
	end
	return ColorSequence.new(keys)
end

-- JSON value -> Roblox value. Tagged tables name the type: {v3={x,y,z}}, {c3={r,g,b}}, ...
local function decode(raw)
	local kind = type(raw)
	if kind == "string" then
		if string.sub(raw, 1, 5) == "Enum." then
			local pieces = string.split(raw, ".")
			return Enum[pieces[2]][pieces[3]]
		end
		return raw
	elseif kind ~= "table" then
		return raw
	end
	if raw.v3 then
		return Vector3.new(raw.v3[1], raw.v3[2], raw.v3[3])
	elseif raw.v2 then
		return Vector2.new(raw.v2[1], raw.v2[2])
	elseif raw.c3 then
		return Color3.new(raw.c3[1], raw.c3[2], raw.c3[3])
	elseif raw.rgb then
		return Color3.fromRGB(raw.rgb[1], raw.rgb[2], raw.rgb[3])
	elseif raw.cf then
		local c = raw.cf
		if #c >= 12 then
			return cframe(c)
		elseif #c >= 6 then
			return CFrame.new(c[1], c[2], c[3]) * CFrame.Angles(math.rad(c[4]), math.rad(c[5]), math.rad(c[6]))
		end
		return CFrame.new(c[1], c[2], c[3])
	elseif raw.range then
		return NumberRange.new(raw.range[1], raw.range[2] or raw.range[1])
	elseif raw.nseq then
		return numberSequence(raw.nseq)
	elseif raw.cseq then
		return colorSequence(raw.cseq)
	elseif raw.udim2 then
		return UDim2.new(raw.udim2[1], raw.udim2[2], raw.udim2[3], raw.udim2[4])
	elseif raw.udim then
		return UDim.new(raw.udim[1], raw.udim[2])
	end
	return raw
end

local function create(spec, context)
	local ok, instance = pcall(Instance.new, spec.class or "Folder")
	if not ok then
		table.insert(context.notes, "cannot create a " .. tostring(spec.class) .. ": " .. tostring(instance))
		return nil
	end
	local name = spec.name or spec.class
	instance.Name = name
	if instance:IsA("BasePart") then
		instance.Anchored = true -- effects and props stay put unless the tree says otherwise
		table.insert(context.parts, instance)
	end
	for key, raw in pairs(spec.props or {}) do
		if type(raw) == "table" and raw.ref then
			table.insert(context.pending, { instance = instance, key = key, ref = raw.ref, name = name })
		else
			local set, problem = pcall(function()
				instance[key] = decode(raw)
			end)
			if not set then
				if key == "Source" then
					context.scriptsBlocked = true
				end
				table.insert(context.notes, name .. "." .. key .. ": " .. tostring(problem))
			end
		end
	end
	for key, raw in pairs(spec.attributes or {}) do
		pcall(function()
			instance:SetAttribute(key, decode(raw))
		end)
		if key == "Spin" or key == "Pulse" or key == "Flicker" then
			context.animated = true
		end
	end
	if instance:GetAttribute("Pulse") then      -- remember the resting value: pulsing must not drift
		if instance:IsA("BasePart") then
			instance:SetAttribute("BaseSize", instance.Size)
		elseif instance:IsA("Light") then
			instance:SetAttribute("BaseBrightness", instance.Brightness)
		end
	end
	if spec.id then
		context.refs[spec.id] = instance
	end
	for _, child in ipairs(spec.children or {}) do
		local made = create(child, context)
		if made then
			made.Parent = instance
		end
	end
	context.created = context.created + 1
	return instance
end

local function runTree(job)
	local parent = resolve(job.parent or "Workspace")
	if not parent then
		error("no such place to build in: " .. tostring(job.parent))
	end
	local origin
	local existing = nil
	for _, child in ipairs(parent:GetChildren()) do
		if child:GetAttribute("UnixAsset") == job.asset then
			existing = child
			break
		end
	end
	if existing then
		origin = existing:GetAttribute("UnixOrigin")
		existing:Destroy()
	end
	if not origin then
		origin = (job.place == "camera") and dropPoint() or Vector3.new(0, 0, 0)
	end
	local context = { refs = {}, pending = {}, notes = {}, parts = {}, created = 0, scriptsBlocked = false }
	local root = create(job.root, context)
	if not root then
		error("the root could not be created")
	end
	for _, item in ipairs(context.pending) do
		local target = context.refs[item.ref]
		local set, problem = pcall(function()
			item.instance[item.key] = target
		end)
		if not target or not set then
			table.insert(context.notes, item.name .. "." .. item.key .. ": no node with id '" .. tostring(item.ref) .. "' " .. tostring(problem or ""))
		end
	end
	for _, part in ipairs(context.parts) do
		part.CFrame = part.CFrame + origin
	end
	root:SetAttribute("UnixAsset", job.asset)
	root:SetAttribute("UnixOrigin", origin)
	if context.animated then
		root:SetAttribute("UnixAnimated", true)
		local animator = Instance.new("Script")
		animator.Name = "UnixAnimator"
		local written = pcall(function()
			animator.Source = ANIMATOR
		end)
		if written then
			animator.Parent = root
		else
			context.scriptsBlocked = true
		end
	end
	root.Parent = parent
	local moving = context.animated and watch(root) or 0
	pcall(function()
		ChangeHistoryService:SetWaypoint("Unix: " .. job.asset)
	end)
	pcall(function()
		-- not selected: the selection box and move handles would cover the effect in screenshots
		local selection = game:GetService("Selection")
		local kept = {}
		for _, item in ipairs(selection:Get()) do
			if item.Parent ~= nil and not item:IsDescendantOf(root) then
				table.insert(kept, item)
			end
		end
		selection:Set(kept)
	end)
	if context.scriptsBlocked then
		table.insert(context.notes, "Studio blocked writing script text. Allow script injection for Unix Bridge (Plugins > Manage Plugins), then build again.")
	end
	return { created = context.created, notes = context.notes, model = root:GetFullName(), place = placeName(),
		origin = { origin.X, origin.Y, origin.Z }, replaced = existing ~= nil, animated = moving }
end

local function describe(instance, depth, props)
	local node = { name = instance.Name, class = instance.ClassName }
	if props and #props > 0 then
		node.props = {}
		for _, key in ipairs(props) do
			local ok, found = pcall(function()
				return instance[key]
			end)
			if ok then
				node.props[key] = tostring(found)
			end
		end
	end
	local children = instance:GetChildren()
	if depth > 0 and #children > 0 then
		node.children = {}
		for index, child in ipairs(children) do
			if index > 80 then
				node.more = #children - 80
				break
			end
			table.insert(node.children, describe(child, depth - 1, props))
		end
	elseif #children > 0 then
		node.childCount = #children
	end
	return node
end

local function runQuery(job)
	local instance = resolve(job.path or "Workspace")
	if not instance then
		error("nothing at " .. tostring(job.path))
	end
	return { place = placeName(), tree = describe(instance, job.depth or 1, job.props) }
end

local function runRemove(job)
	local instance = resolve(job.path)
	if not instance then
		error("nothing at " .. tostring(job.path))
	end
	if instance.Parent == game or instance == Workspace.CurrentCamera or instance:IsA("Terrain") then
		error("refusing to remove " .. instance:GetFullName())
	end
	local name = instance:GetFullName()
	instance:Destroy()
	pcall(function()
		ChangeHistoryService:SetWaypoint("Unix: removed " .. name)
	end)
	return { removed = name, place = placeName() }
end

local function runCamera(job)
	local instance = resolve(job.path)
	if not instance then
		error("nothing at " .. tostring(job.path))
	end
	local centre, size
	if instance:IsA("Model") then
		local box, extents = instance:GetBoundingBox()
		centre, size = box.Position, extents
	elseif instance:IsA("BasePart") then
		centre, size = instance.Position, instance.Size
	else
		error(instance.ClassName .. " has no position to look at")
	end
	local reach = job.distance or (size.Magnitude * 1.1 + 6)
	local camera = Workspace.CurrentCamera
	local direction = Vector3.new(1, 0.55, 1).Unit
	local from = job.direction -- where to look from, when the default side is the one in shadow
	if type(from) == "table" and #from == 3 and Vector3.new(from[1], from[2], from[3]).Magnitude > 0.001 then
		direction = Vector3.new(from[1], from[2], from[3]).Unit
	end
	camera.CFrame = CFrame.lookAt(centre + direction * reach, centre)
	camera.Focus = CFrame.new(centre)
	return { looking_at = instance:GetFullName(), distance = reach, place = placeName() }
end

local function run(job)
	if job.kind == "tree" then
		return runTree(job)
	elseif job.kind == "query" then
		return runQuery(job)
	elseif job.kind == "known" then -- what this place already holds: Unix does not send it again
		local meshes, images = uploadedBefore()
		local meshKeys, pictureKeys = {}, {}
		for key in pairs(meshes) do
			table.insert(meshKeys, key)
		end
		for key in pairs(images) do
			table.insert(pictureKeys, key)
		end
		return { meshes = meshKeys, pictures = pictureKeys, notes = {}, place = placeName() }
	elseif job.kind == "remove" then
		return runRemove(job)
	elseif job.kind == "camera" then
		return runCamera(job)
	elseif job.kind == "picture" then
		return runPicture(job)
	elseif job.kind == "motion" then
		return runMotion(job)
	end
	local report = { parts = 0, meshes = 0, effects = 0, uploaded = 0, reused = 0, kept = 0, preview = 0, notes = {},
		place = placeName() }
	local entry = assetOf(job)
	if job.rigs then -- the skeletons this asset's skinned meshes hang on (see rigFor)
		entry.rigs = job.rigs
	end
	local upload = job.upload == true
	buildParts(job, entry, report)
	local meshes = job.meshes or {}
	-- first every new shape and every new picture, side by side (each is made and uploaded once) ...
	local shapes, pictures, seenShape, seenPicture = {}, {}, {}, {}
	for _, item in ipairs(meshes) do
		if item.key and item.v and not seenShape[item.key] and not entry.shapes[item.key] then
			seenShape[item.key] = true
			table.insert(shapes, item)
		end
		local texture = item.texture
		if texture and texture.key and texture.data and not seenPicture[texture.key] and not entry.images[texture.key] then
			seenPicture[texture.key] = true
			table.insert(pictures, item)
		end
	end
	local clock = os.clock()
	local timing = {}
	local function lap(name)
		local now = os.clock()
		timing[name] = (timing[name] or 0) + (now - clock)
		clock = now
	end
	lap("prepare")
	sideBySide(pictures, 4, function(item)
		local ok, problem = pcall(imageFor, item, entry, upload, report)
		if not ok then
			table.insert(report.notes, "texture of " .. tostring(item.name) .. " failed: " .. tostring(problem))
		end
	end)
	lap("textures")
	sideBySide(surfaces.wanted(meshes, entry), 4, function(wanted)
		local ok, problem = pcall(surfaces.picture, wanted.key, entry, upload, report, wanted.name)
		if not ok then
			table.insert(report.notes, "picture " .. wanted.name .. " failed: " .. tostring(problem))
		end
	end)
	lap("pictures")
	sideBySide(shapes, 8, function(item)
		local ok, problem = pcall(shapeFor, item, entry, upload, report)
		if not ok then
			table.insert(report.notes, "mesh " .. tostring(item.name) .. " failed: " .. tostring(problem))
		end
	end)
	lap("shapes")
	-- ... then the parts that use them, also side by side: loading a mesh asset is a wait too
	sideBySide(meshes, 8, function(item)
		local ok, problem = pcall(buildMesh, item, entry, upload, report)
		if not ok then
			table.insert(report.notes, "mesh " .. tostring(item.name) .. " failed: " .. tostring(problem))
		end
	end)
	lap("meshes")
	report.timing = timing
	for _, item in ipairs(job.effects or {}) do
		local ok, problem = pcall(buildEffect, item, entry, report, upload)
		if not ok then
			table.insert(report.notes, "effect " .. tostring(item.name) .. " failed: " .. tostring(problem))
		end
	end
	if job.last then
		surfaces.done(entry)
		if entry.animated then      -- the same animator that plays Spin / Pulse / Flicker plays the tracks
			entry.model:SetAttribute("UnixAnimated", true)
			if not entry.model:FindFirstChild("UnixAnimator") then
				local animator = Instance.new("Script")
				animator.Name = "UnixAnimator"
				local written = pcall(function()
					animator.Source = ANIMATOR
				end)
				if written then
					animator.Parent = entry.model
				else
					table.insert(report.notes, "Studio blocked writing the animator script: the motion plays in Edit mode only. Allow script injection for Unix Bridge (Plugins > Manage Plugins).")
				end
			end
			watch(entry.model)
		end
		pcall(function()
			ChangeHistoryService:SetWaypoint("Unix: " .. job.asset)
		end)
		pcall(function()
			game:GetService("Selection"):Set({ entry.model })
		end)
	end
	report.model = entry.model:GetFullName()
	return report
end

---------------------------------------------------------------------------------------------
-- link loop: ask every Unix on this computer for work

local function serve(port)
	-- a play test's own copy of the place can load this plugin a moment before it starts running:
	-- it must never be offered as a place to send to
	while current() and not RunService:IsRunning() do
		local reply = request(port, "/unix/poll", {
			session = SESSION, place = placeName(), placeId = game.PlaceId, gameId = game.GameId, version = VERSION,
			testing = testing(),
		})
		if reply == nil then
			status = "waiting for Unix"
			task.wait(3)
		else
			status = "linked to Unix"
			local job = reply.job
			if job then
				local ok, result = pcall(run, job)
				request(port, "/unix/result", {
					session = SESSION, job = job.id, ok = ok,
					report = ok and result or nil, message = (not ok) and tostring(result) or nil,
				})
			end
			task.wait(0.05)
		end
	end
end

for _, port in ipairs(PORTS) do
	task.spawn(serve, port)
end
print("[Unix Bridge] v" .. VERSION .. " ready. Models and effects sent from Unix will appear in this place.")
